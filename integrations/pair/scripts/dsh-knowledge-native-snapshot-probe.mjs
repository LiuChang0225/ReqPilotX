/** Keyless real-DSH Session probe for selected evidence consumption and refusal. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { installTaskMcp } from '../host/src/task-mcp.js';
import { TaskLauncher } from '../host/src/task-launch.js';
import { KnowledgeBridge } from '../host/src/knowledge.js';

export const name = 'reqpilot-knowledge-native-probe';
export const inject = ['sessionController', 'workspaceController', 'agentPresets', 'subprocess', 'agentDefaultModel'];

const digest = value => createHash('sha256').update(Buffer.isBuffer(value) ? value : typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');

function fakeKnowledge(applicationId) {
  const base = { id: 'kb-project', name: 'Project references',
    owner: { kind: 'application-project', applicationId, projectId: 'p' } };
  const document = { id: 'project-doc', kbId: 'kb-project', name: 'internal.txt', mime: 'text/plain',
    state: 'indexed', sha256: '1'.repeat(64), backendRevision: '2'.repeat(64),
    expectedPages: 1, pages: [{ number: 1, text: 'The measured standby current was 4 mA at 12 V and 25 C.' }],
    wikiState: 'not-requested' };
  const selections = new Map(); let searches = 0;
  const capture = caller => {
    const ids = selections.get(caller) || [];
    if (!ids.includes('kb-project')) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
    return { version: 1, caller, kbIds: ['kb-project'], documents: [{ kbId: 'kb-project',
      documentId: document.id, name: document.name, mime: document.mime,
      originalRevision: document.sha256, extractionRevision: document.backendRevision,
      coverage: 'complete', location: 'logical-section', pages: 1, expectedPages: 1,
      wiki: 'not-requested' }] };
  };
  return {
    applicationId, document, selections, get searches() { return searches; },
    snapshot: () => ({ bases: [base], documents: [document], jobs: [] }),
    getProjectReferenceLibrary: owner => owner.projectId === 'p' ? base : undefined,
    ensureProjectReferenceLibrary: async () => base,
    select: async (caller, ids) => { selections.set(caller, [...ids]); },
    captureSelectedSources: async caller => capture(caller),
    searchSelectedSources: async (caller) => {
      capture(caller); searches += 1;
      const hit = { id: 'hit-' + searches, kbId: 'kb-project', documentId: document.id,
        title: document.name, originalRevision: document.sha256,
        extractionRevision: document.backendRevision, coverage: 'complete',
        location: { kind: 'logical-section', number: 1 },
        text: document.pages[0].text, score: 1, url: '/provider/hit' };
      // The second evidence read must observe a changed exact version rather
      // than silently reading a newer extraction.
      document.backendRevision = '3'.repeat(64);
      return [hit];
    },
    readSelectedSources: async () => { throw Object.assign(Error('not-used'), { code: 'not-used' }); },
    selectTopicVersion: async () => {},
    topicSelection: () => undefined,
    topicVersions: async () => [],
    readTopicVersion: async () => { throw Error('not-used'); },
    topicOriginal: async () => { throw Error('not-used'); },
    original: async () => ({ document, bytes: Buffer.from(document.pages[0].text) }),
    importProjectReferenceSource: async () => { throw Error('not-used'); },
  };
}

function sse(res, value) { res.write('data: ' + JSON.stringify(value) + '\n\n'); }
function chunk(extra) { return { id: 'chatcmpl-reqpilot-probe', object: 'chat.completion.chunk', created: 1, model: 'noop', ...extra }; }

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
  const sourceSha = digest(bytes);
  const dataRoot = join(output, 'owned'); await mkdir(join(dataRoot, 'task-launches'), { recursive: true, mode: 0o700 });

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
      const child = ctx.subprocess.spawn({ argv, cwd: dataRoot, env,
        stdio: { stdin: 'ignore', stdout: { maxBytes: 65536 }, stderr: { maxBytes: 65536 } }, graceMs: 5000 });
      const result = await child.done;
      if (result.exitCode !== 0) throw Error(child.collected.stdout.readFrom(0).text + child.collected.stderr.readFrom(0).text);
      return child.collected.stdout.readFrom(0).text;
    } };
  const applicationId = 'reqpilotx-pair:' + runtime.ownerId;
  const knowledgeService = fakeKnowledge(applicationId);
  const bridge = new KnowledgeBridge({ service: knowledgeService, runtime,
    dataRoot: join(dataRoot, 'knowledge'), applicationId });
  const sessions = ctx.get('sessionController'), workspaces = ctx.get('workspaceController');
  const launcher = new TaskLauncher(runtime, { capabilityRoot, agentPreset: 'standard', taskMcp: true },
    () => ({ workspaces, defaultModel: ctx.get('agentDefaultModel'), sessions }), fetch, bridge);
  installTaskMcp(ctx, launcher);
  try {
    await launcher.initialize();
    const receipt = await launcher.start({ commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha,
      goal: 'Read only the selected project evidence and refuse a changed version.',
      knowledge: { mode: 'PROJECT_REFERENCES', projectDocumentIds: ['project-doc'] } });
    let inspection;
    for (let index = 0; index < 900; index += 1) {
      inspection = await sessions.inspect(receipt.sessionId);
      const ended = inspection.events.filter(event => event.type === 'turn/end');
      if (ended.length) {
        if (ended.at(-1).data.reason.kind !== 'completed') throw Error('Scripted turn failed: ' + JSON.stringify(ended.at(-1).data.reason));
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (!inspection || !inspection.events.some(event => event.type === 'turn/end')) throw Error('Scripted DSH turn did not finish');
    const results = inspection.events.filter(event => event.type === 'tool/result').map(event => JSON.stringify(event.data?.message || {}));
    const success = results.find(value => value.includes('pair-fact-'));
    const refusal = results.find(value => value.includes('KNOWLEDGE_EVIDENCE_CHANGED'));
    if (!success) throw Error('The selected evidence tool did not log a consumed fact: ' + results.join('\n').slice(0, 2000));
    if (!refusal) throw Error('The changed-version evidence request was not refused: ' + results.join('\n').slice(0, 2000));
    const header = inspection.events.find(event => event.type === 'request/header')?.data?.header;
    const tools = (header?.tools || []).map(tool => tool.name);
    if (!tools.includes('mcp__reqpilot_knowledge__knowledge_evidence_search')) throw Error('knowledge evidence tool was not visible before the first request');
    const assistant = inspection.events.filter(event => event.type === 'assistant/message').map(event => JSON.stringify(event.data)).join('\n');
    if (!assistant.includes('EVIDENCE_SCENARIO_DONE')) throw Error('Scripted final text missing');
    const eventBody = JSON.stringify({ session: inspection.session, events: inspection.events }, null, 2);
    await writeFile(join(output, 'session-events.json'), eventBody + '\n');
    await writeFile(join(output, 'scenario.json'), JSON.stringify({ schema: 'reqpilot-rx04-knowledge-native-scenario/v1',
      source_sha256: sourceSha, task_id: receipt.taskId, session_id: receipt.sessionId,
      scenario: 'knowledge-evidence', mcp_tools_before_first_request: tools,
      selected_evidence_result_logged: Boolean(success), changed_version_refusal_logged: Boolean(refusal),
      knowledge_searches: knowledgeService.searches,
      expected: { selected_source: 'only project-doc', changed_versions: 'REFUSED_NO_RETARGET' },
      projection: 'PUBLIC_SESSION_INSPECT_EVENTS' }, null, 2) + '\n');
    const result = { status: 'PASS', schema: 'reqpilot-rx04-knowledge-native-session/v1',
      task_id: receipt.taskId, session_id: receipt.sessionId, work_ref: receipt.workRef,
      source_sha256: sourceSha, selected_evidence_result_logged: true,
      changed_version_refusal_logged: true, knowledge_searches: knowledgeService.searches,
      session_event_count: inspection.events.length,
      session_events_sha256: digest(eventBody), mcp_tools_before_first_request: tools,
      prose_advanced_workflow: false, mock_openai_http: true, real_model: false, remote_model_calls: 0,
      limitation: 'Loopback OpenAI-compatible scripted endpoint and real DSH Session/Workspace/MCP; synthetic PAIR contract fixture, no provider model, customer document or UI acceptance.' };
    await writeFile(join(output, 'receipt.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    await launcher.close();
    await bridge.close();
    await new Promise(resolve => service.close(resolve));
  }
}
