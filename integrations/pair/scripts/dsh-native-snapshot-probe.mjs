/** Real DSH Session probe against a loopback OpenAI-compatible scripted endpoint. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { installTaskMcp } from '../host/src/task-mcp.js';
import { TaskLauncher } from '../host/src/task-launch.js';

export const name = 'reqpilot-native-snapshot-probe';
export const inject = ['sessionController', 'workspaceController', 'agentPresets', 'subprocess', 'agentDefaultModel'];

function sse(res, value) {
  res.write('data: ' + JSON.stringify(value) + '\n\n');
}

function chunk(extra) {
  return { id: 'chatcmpl-reqpilot-probe', object: 'chat.completion.chunk', created: 1,
    model: 'noop', ...extra };
}

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
  const commandId = process.env.REQPILOT_NATIVE_COMMAND;
  const bytes = await readFile(join(output, 'synthetic.pdf'));
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
    const receipt = await launcher.inspect(commandId);
    let inspection;
    for (let i = 0; i < 900; i++) {
      inspection = await sessions.inspect(receipt.sessionId);
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
    const record = JSON.parse(await readFile(join(dataRoot, 'task-launches', commandId + '.json'), 'utf8'));
    const taskRoot = record.prepared?.task_root;
    const checkReceipt = taskRoot ? join(taskRoot, 'workflow-check.json') : null;
    const earlyCheck = checkReceipt && existsSync(checkReceipt) ? await readFile(checkReceipt) : null;
    const toolResults = inspection.events.filter(event => event.type === 'tool/result')
      .map(event => event.data?.message).filter(Boolean);
    const taskOpen = toolResults.find(message => JSON.stringify(message).includes('RESUMED'));
    const requestHeader = inspection.events.find(event => event.type === 'request/header')?.data?.header;
    const mcpTools = (requestHeader?.tools || []).map(tool => tool.name).filter(name => name.startsWith('mcp__reqpilot__'));
    if (!mcpTools.includes('mcp__reqpilot__task_open')) throw Error('task_open MCP tool was not visible before the first model request');
    const assistantText = inspection.events.filter(event => event.type === 'assistant/message')
      .map(event => JSON.stringify(event.data)).join('\n');
    const checkResults = toolResults.filter(message => JSON.stringify(message).includes('CHECKED'));
    if (!checkResults.length) throw Error('early task_check result missing');
    if (!earlyCheck) throw Error('early task_check receipt was not retained');
    const earlyCheckState = JSON.parse(earlyCheck);
    if (earlyCheckState.status !== 'UNCONFIRMED' || earlyCheckState.ready_to_review === true)
      throw Error('early task_check claimed a configured/completed state');
    const sourceModel = final.selectedAgentModel || final.modelSelection;
    if (sourceModel?.provider !== 'reqpilot-scripted' || sourceModel?.model !== 'noop' || sourceModel?.reasoningEffort !== 'max')
      throw Error('Default model route did not reach the Session: ' + JSON.stringify(sourceModel));
    if (!taskOpen) throw Error('task_open MCP result (RESUMED) missing');
    if (!assistantText.includes('PROBE_ONLY')) throw Error('Scripted final text missing');
    if (!workflow || workflow.stages[0]?.status !== 'SAVED' || workflow.stages[1]?.status !== 'PENDING'
      || workflow.ready_to_review !== false) throw Error('Scripted prose/idle advanced workflow state: ' + JSON.stringify(workflow));
    if (workflow.stages.slice(2).some(stage => stage.status === 'SAVED')) throw Error('Missing business artifacts were reported saved');
    const eventBody = JSON.stringify({ session: inspection.session, events: inspection.events }, null, 2);
    await writeFile(join(output, 'session-events.json'), eventBody + '\n');
    await writeFile(join(output, 'workflow-state.json'), JSON.stringify(workflow, null, 2) + '\n');
    const scenario = { schema: 'reqpilot-rx02-native-scenario/v1',
      source_sha256: sourceSha, task_id: final.taskId, session_id: final.sessionId,
      model_selection: final.selectedAgentModel, request_header_model: requestHeader?.config,
      mcp_tools_before_first_request: mcpTools,
      scripted_turns: [
        { step: 1, assistant: { kind: 'tool-call', name: 'mcp__reqpilot__task_open',
          arguments: { mode: 'resume', task_id: final.taskId } }, tool_result_status: 'RESUMED' },
        { step: 2, assistant: { kind: 'tool-call', name: 'mcp__reqpilot__task_check',
          arguments: { task_id: final.taskId } }, tool_result_status: 'CHECKED', check_status: earlyCheckState.status },
        { step: 3, assistant: { kind: 'text', text: 'PROBE_ONLY: early check retained; no business artifacts were saved by this message.' } },
      ],
      expected_workflow: { PREPARATION: 'SAVED', SOURCE_MAP: 'PENDING', ITEMS_CLASSIFICATION: 'BLOCKED',
        EVIDENCE_ANALYSIS: 'BLOCKED', SELF_CHECK: 'BLOCKED', DELIVERABLE: 'BLOCKED', ready_to_review: false },
      observed_workflow: Object.fromEntries(workflow.stages.map(stage => [stage.id, stage.status])),
      projection: 'PUBLIC_SESSION_INSPECT_EVENTS; exact event body hash in receipt' };
    await writeFile(join(output, 'scenario.json'), JSON.stringify(scenario, null, 2) + '\n');
    const result = { status: 'PASS', schema: 'reqpilot-rx02-native-session/v1',
      task_id: final.taskId, session_id: final.sessionId, work_ref: final.workRef,
      command_id: commandId, source_sha256: sourceSha, model_selection: final.modelSelection,
      selected_agent_model: final.selectedAgentModel, session_event_count: inspection.events.length,
      session_events_sha256: createHash('sha256').update(eventBody).digest('hex'),
      mcp_task_open_resumed: true, early_check_retained: true,
      early_check_receipt: checkReceipt, early_check_sha256: earlyCheck ? createHash('sha256').update(earlyCheck).digest('hex') : null,
      mcp_tools_before_first_request: mcpTools,
      request_header_model: requestHeader?.config, workflow_before: workflow,
      prose_advanced_workflow: false, ready_to_review: false,
      scripted_model_adapter: false, mock_openai_http: true, real_model: false, remote_model_calls: 0,
      limitation: 'Loopback OpenAI-compatible scripted endpoint and real DSH Session/Workspace/MCP; no provider model, customer document or business extraction.' };
    await writeFile(join(output, 'receipt.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    await launcher.close();
    await new Promise(ok => service.close(ok));
  }
}
