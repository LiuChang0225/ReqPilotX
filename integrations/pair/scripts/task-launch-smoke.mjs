/** Real frozen-package preparation against a synthetic HTTP peer; DSH admission is mocked, no model call. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { TaskLauncher } from '../host/src/task-launch.js';
import { nativeSubprocess } from '../tests/native-subprocess.mjs';
const [pythonArg, bundleArg, outputArg] = process.argv.slice(2);
if (!outputArg) throw Error('Usage: task-launch-smoke.mjs <python> <frozen-development-bundle> <private-output>');
const python = resolve(pythonArg), capabilityRoot = resolve(bundleArg), output = resolve(outputArg);
await mkdir(output, { recursive: true, mode: 0o700 });
const source = join(output, 'synthetic.pdf');
execFileSync(python, ['-c', 'from pypdf import PdfWriter;import sys;w=PdfWriter();w.add_blank_page(200,200);w.write(sys.argv[1])', source]);
const bytes = await readFile(source), sourceSha = createHash('sha256').update(bytes).digest('hex');
const works = new Map(), firstBindingAttempts = [];
const server = createServer(async (req, res) => {
  const send = value => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
  if (req.url === '/api/projects/p/sor-documents') { send([{ id: 'd', file_name: 'synthetic.pdf', source_sha256: sourceSha }]); return; }
  if (req.url.endsWith('/file')) { res.writeHead(200, { 'content-type': 'application/pdf' }); res.end(bytes); return; }
  if (req.method === 'POST' && req.url.endsWith('/plugin-workbench')) {
    let body = ''; for await (const chunk of req) body += chunk; const payload = JSON.parse(body); firstBindingAttempts.push(payload.session_id);
    if (!works.has(payload.session_id)) works.set(payload.session_id, { schema_version: 'reqpilot-plugin-workbench/v1', session_id: payload.session_id,
      project_id: 'p', document_id: 'd', source_sha256: sourceSha, revision_id: null, revision_number: 0,
      map_confirmation_required: false, confirmed_map_sha256: null, stages: [], versions: [], feedback: [] });
    if (firstBindingAttempts.length === 1) { res.destroy(); return; }
    send(works.get(payload.session_id)); return;
  }
  const state = works.get(req.url.split('/').at(-1)); if (state) send(state); else { res.writeHead(404); res.end(); }
});
server.listen(0, '127.0.0.1'); await once(server, 'listening');
const runtime = { dataRoot: join(output, 'owned'), python, ownerId: randomUUID(), snapshot: { mode: 'managed', status: 'READY', serviceOrigin: 'http://127.0.0.1:' + server.address().port }, async command(argv, env = {}) {
  await mkdir(this.dataRoot, { recursive: true }); const h = nativeSubprocess.spawn({ argv, cwd: this.dataRoot, env }); const result = await h.done;
  if (result.exitCode !== 0) throw Error(h.collected.stdout.readFrom(0).text + h.collected.stderr.readFrom(0).text);
  return h.collected.stdout.readFrom(0).text;
} };
const acceptedIds = new Set(), sessionIds = new Set(); let promptAttempts = 0;
const services = { workspaces: { async create() { return { workspace: { workspaceId: 'workspace-smoke' } }; } }, sessions: {
  async create(value) { sessionIds.add(value.sessionId); return { sessionId: value.sessionId }; },
  async prompt(value) { promptAttempts++; acceptedIds.add(value.requestId); if (promptAttempts === 1) throw Error('mock DSH acknowledgement lost'); return { accepted: true }; },
} };
const launcher = new TaskLauncher(runtime, { capabilityRoot }, () => services);
try {
  await launcher.initialize(); const input = { commandId: randomUUID(), projectId: 'p', documentId: 'd', sourceSha256: sourceSha, goal: 'Inspect this synthetic source; preserve its content.' };
  await assert.rejects(launcher.start(input), /acknowledgement lost/); // shared helper repairs lost HTTP reply, then mocked DSH acknowledgement is lost
  const final = await launcher.start(input); assert.equal(final.status, 'ACCEPTED'); assert.equal(works.size, 1);
  assert.equal(firstBindingAttempts.length, 2); assert.equal(firstBindingAttempts[0], firstBindingAttempts[1]);
  assert.equal(sessionIds.size, 1); assert.equal(acceptedIds.size, 1);
  const workspace = join(runtime.dataRoot, 'task-workspaces', input.commandId);
  const taskText = await readFile(join(workspace, 'request.md'), 'utf8'); assert.match(taskText, /TASK.md/); assert.match(taskText, new RegExp(final.taskId));
  const receipt = { status: 'PASS', source_sha256: sourceSha, result: final, first_binding_attempts: firstBindingAttempts,
    task_brief_from_shared_core: true, unique_remote_works: works.size, unique_mock_sessions: sessionIds.size,
    mock_prompt_attempts: promptAttempts, unique_mock_request_ids: acceptedIds.size, model_calls: 0,
    limitation: 'Real shared frozen-package CLI/core and synthetic HTTP; Pair controller admission is mocked, not a live Pair/model acceptance test.' };
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n'); console.log(JSON.stringify(receipt));
} finally { await launcher.close(); await new Promise(ok => server.close(ok)); }
