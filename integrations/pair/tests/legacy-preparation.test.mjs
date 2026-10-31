import test from 'node:test';
import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { TaskLauncher } from '../host/src/task-launch.js';

const python = process.env.PAIR_RECONNECT_PYTHON;
const sourceBundle = process.env.PAIR_LEGACY_BUNDLE;
const enabled = Boolean(python && sourceBundle);
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value) ? value.map(stable) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])])) : value;
const canonical = value => sha(JSON.stringify(stable(value)));

test('legacy v1 recorded retry prepares with goal only and never substitutes the new helper', { skip: !enabled }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-legacy-prep-'));
  try {
    const legacy = join(root, 'legacy-bundle');
    await cp(sourceBundle, legacy, { recursive: true });
    const oldHelper = execFileSync('git', ['show', '57f2e73:plugins/reqpilot/scripts/host_launch.py'], { encoding: 'utf8' });
    await writeFile(join(legacy, 'scripts/host_launch.py'), oldHelper);
    const receipt = JSON.parse(await readFile(join(legacy, 'build-receipt.json'), 'utf8'));
    for (const entry of receipt.plugin_files) if (entry.path === 'scripts/host_launch.py') entry.sha256 = sha(oldHelper);
    delete receipt.bundle_sha256;
    receipt.bundle_sha256 = canonical(receipt);
    await writeFile(join(legacy, 'build-receipt.json'), JSON.stringify(receipt));
    const bundleSha = receipt.bundle_sha256;
    const source = Buffer.from('legacy synthetic source'), sourceSha = sha(source);
    let binding;
    const backend = createServer(async (req, res) => {
      const send = value => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
      if (req.url.endsWith('/sor-documents')) { send([{ id: 'd', file_name: 'legacy.pdf', source_sha256: sourceSha }]); return; }
      if (req.url.endsWith('/file')) { res.writeHead(200); res.end(source); return; }
      if (req.method === 'POST' && req.url.endsWith('/plugin-workbench')) {
        let body = ''; for await (const part of req) body += part; const input = JSON.parse(body);
        binding = { session_id: input.session_id, source_sha256: sourceSha, project_id: 'p', document_id: 'd',
          revision_id: null, revision_number: 0, map_confirmation_required: false, confirmed_map_sha256: null,
          stages: [], versions: [], feedback: [] };
        send(binding); return;
      }
      if (req.url.includes('/plugin-workbench/')) { send(binding || { session_id: 'plugin-' + 'a'.repeat(32), source_sha256: sourceSha, project_id: 'p', document_id: 'd', revision_id: null, revision_number: 0, map_confirmation_required: false, stages: [], versions: [], feedback: [] }); return; }
      res.writeHead(404); res.end();
    });
    backend.listen(0, '127.0.0.1'); await once(backend, 'listening');
    let promptCount = 0;
    const services = { workspaces: { async create() { return { workspace: { workspaceId: 'workspace-legacy' } }; } },
      sessions: { async create(value) { return { sessionId: value.sessionId }; },
        async prompt() { promptCount++; if (promptCount === 1) throw Error('legacy ack lost'); return { accepted: true }; } } };
    const runtime = { dataRoot: root, python, ownerId: randomUUID(),
      snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:' + backend.address().port },
      async command(argv, env = {}) {
        const child = spawn(argv[0], argv.slice(1), { cwd: root, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
        let out = '', err = ''; child.stdout.on('data', x => out += x); child.stderr.on('data', x => err += x);
        const code = await new Promise((ok, fail) => { child.once('error', fail); child.once('close', ok); });
        if (code !== 0) throw Error(out + err);
        return out;
      } };
    const commandId = randomUUID(), goal = 'Legacy recorded goal only.';
    const applicationId = 'reqpilotx-pair:' + runtime.ownerId;
    const fingerprint = sha(JSON.stringify(['p', 'd', sourceSha, goal, false]));
    await mkdir(join(root, 'task-launches'), { recursive: true });
    await writeFile(join(root, 'task-launches', commandId + '.json'), JSON.stringify({
      schema: 'reqpilot-pair-launch-record/v1', commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha,
      goal, requireMapConfirmation: false, fingerprint, applicationId, serviceOrigin: runtime.snapshot.serviceOrigin,
      serviceInstanceId: null, sessionId: 'session-legacy', requestId: 'reqpilotx-' + commandId, phase: 'RECORDED',
      bundleSha, capabilityRoot: legacy, python, workspaceRelative: 'task-workspaces/' + commandId,
      agentPreset: 'standard', toolTransport: 'CLI' }));
    const launcher = new TaskLauncher(runtime, { capabilityRoot: legacy }, () => services);
    launcher.ready = true; launcher.capabilityRoot = legacy; launcher.bundleSha = bundleSha;
    try {
      await assert.rejects(launcher.start({ commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha, goal,
        requireMapConfirmation: false }), /legacy ack lost/);
      const prepare = JSON.parse(await readFile(join(root, 'task-launches', commandId + '-input/prepare.json'), 'utf8'));
      assert.equal(prepare.request.goal, goal);
      assert.equal('supplementary_instructions' in prepare.request, false);
      assert.equal('workflow' in prepare.request, false);
      const result = await launcher.start({ commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha, goal,
        requireMapConfirmation: false });
      assert.equal(result.status, 'ACCEPTED'); assert.equal(result.schema, 'reqpilot-pair-launch/v1');
      assert.equal(promptCount, 2);
      const record = JSON.parse(await readFile(join(root, 'task-launches', commandId + '.json'), 'utf8'));
      assert.equal(record.schema, 'reqpilot-pair-launch-record/v1');
      assert.equal(record.workflow, undefined); assert.equal(record.selectedAgentModel, undefined);
      const context = JSON.parse(await readFile(join(record.prepared.task_root, 'task-context.json'), 'utf8'));
      assert.equal(context.request.goal, goal); assert.equal(context.request.workflow, undefined);
      assert.equal((await readFile(join(record.prepared.workspace, '.reqpilot/scripts/host_launch.py'), 'utf8')).length > 0, true);
    } finally { await launcher.close(); await new Promise(ok => backend.close(ok)); }
  } finally { await rm(root, { recursive: true, force: true }); }
});
