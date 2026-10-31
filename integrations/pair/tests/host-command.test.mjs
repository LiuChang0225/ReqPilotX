import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { cp, mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { basename, join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { KnowledgeBridge } from '../host/src/knowledge.js';

const python = process.env.PAIR_HOST_COMMAND_PYTHON;
const bundle = process.env.PAIR_HOST_COMMAND_BUNDLE;
const legacyBundle = process.env.PAIR_HOST_COMMAND_LEGACY_BUNDLE;
const checkout = fileURLToPath(new URL('../../../', import.meta.url));
const fixture = join(checkout, 'reports/pair-lightweight/artifacts/fixture');
const workRoot = process.env.PAIR_HOST_COMMAND_WORK || join(checkout, 'reports/pair-lightweight/work/16-host-command-run');
const helper = fileURLToPath(new URL('../host/src/task-workflow.py', import.meta.url));
const enabled = Boolean(python && bundle);
const sha = value => createHash('sha256').update(value).digest('hex');
const helperEnv = () => {
  const env = { ...process.env };
  delete env.PYTHONPATH; delete env.PYTHONHOME; delete env.REQPILOT_DEVELOPMENT_ROOT;
  env.PYTHONDONTWRITEBYTECODE = '1'; env.PYTHONNOUSERSITE = '1';
  return env;
};
function run(argv, { cwd, env = {}, signal } = {}) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(argv[0], argv.slice(1), { cwd, env: { ...helperEnv(), ...env }, signal, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', code => resolveRun({ code, stdout, stderr }));
  });
}
async function command(argv, options = {}) {
  const result = await run(argv, options);
  assert.equal(result.code, 0, argv.join(' ') + '\n' + result.stdout + result.stderr);
  return result.stdout;
}
function fakeAdopted() {
  const library = { id: '11111111-1111-4111-8111-111111111111', name: 'Synthetic adopted fixture',
    purpose: 'Host command regression', operation: 'connect-existing', packageVersion: '1.0.0',
    okfVersion: '1.0', versionId: 'b'.repeat(64), state: 'ready', recommendation: 'direct-use',
    addedAt: '2026-10-31T00:00:00.000Z', lastCheckedAt: '2026-10-31T00:00:00.000Z',
    counts: { sources: 1, claims: 1, entities: 0, relations: 0, images: 0, experiences: 0, conflicts: 0, gaps: 0, wikiPages: 0, files: 1 },
    checks: [{ id: 'manifest', state: 'ok', summary: 'Synthetic manifest is complete.' }] };
  const text = 'The valve shall open within 30 ms at 12 V and 23 C.';
  const contentRevision = sha(text);
  const bindings = new Map();
  const caller = value => bindings.get(value);
  return { text, contentRevision,
    adoptedLibraries: () => [{ ...library }],
    bindAdoptedSession: async (callerId, libraryId) => {
      if (libraryId !== library.id) throw Object.assign(Error('not-found'), { code: 'not-found' });
      const value = { sessionId: callerId, libraryId, versionId: library.versionId,
        operation: 'connect-existing', boundAt: '2026-10-31T00:00:00.000Z' };
      bindings.set(callerId, value); return value;
    },
    adoptedBinding: callerId => caller(callerId),
    browseAdopted: async (callerId, section) => {
      if (!caller(callerId)) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
      return { libraryId: library.id, versionId: library.versionId, section, total: 1, truncated: false,
        items: [{ id: 'claim-1', title: 'valve response' }] };
    },
    readAdopted: async (callerId, request) => {
      if (!caller(callerId)) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
      return { libraryId: library.id, versionId: library.versionId, packageVersion: '1.0.0',
        kind: request.kind, id: request.id || request.sourceId || request.path,
        title: 'valve response', record: { id: request.id || 'claim-1' }, contentRevision,
        locator: { kind: 'document' }, availability: 'metadata-only' };
    },
    adoptedEvidence: async (callerId, query, limit) => {
      if (!caller(callerId)) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
      return { version: 1, libraryId: library.id, versionId: library.versionId, packageVersion: '1.0.0',
        query, coverage: { scanned: 1, truncated: false }, warnings: [], insufficiencies: [],
        items: [{ evidenceId: 'claim:claim-1', kind: 'claim', title: 'valve response', excerpt: text,
          packageVersion: '1.0.0', origin: 'derived', contentRevision,
          locator: { kind: 'logical-section', section: '1.1' } }].slice(0, limit) };
    },
    adoptedSource: async () => ({ bytes: Buffer.from('fixture'), mime: 'text/plain', filename: 'fixture.txt',
      availability: 'original', contentRevision: sha('fixture') }),
    adoptedImage: async () => ({ bytes: Buffer.from([1]), mime: 'image/png' }),
  };
}
function fakeProject() {
  const applicationId = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc';
  const text = 'The valve shall open within 30 ms at 12 V and 23 C.';
  const document = { id: 'project-doc', kbId: 'kb-project', name: 'fixture.txt', mime: 'text/plain',
    state: 'indexed', sha256: sha(text), backendRevision: sha(text + ':extract'), expectedPages: 1,
    pages: [{ number: 1, text }], wikiState: 'not-requested' };
  const library = { id: 'kb-project', name: 'Project references',
    owner: { kind: 'application-project', applicationId, projectId: 'p' } };
  const selections = new Map();
  return { applicationId, text,
    snapshot: () => ({ schemaVersion: 1, bases: [{ ...library }], documents: [{ ...document }], jobs: [], selections: {}, wikiEdits: [] }),
    getProjectReferenceLibrary: owner => owner.projectId === 'p' ? library : undefined,
    ensureProjectReferenceLibrary: async () => library,
    select: async (caller, ids) => { selections.set(caller, [...ids]); },
    captureSelectedSources: async (caller, pairs) => ({ version: 1, caller, kbIds: [...(selections.get(caller) || [])],
      documents: pairs.map(({ kbId, documentId }) => ({ kbId, documentId, name: document.name, mime: document.mime,
        originalRevision: document.sha256, extractionRevision: document.backendRevision, coverage: 'complete',
        location: 'logical-section', pages: 1, expectedPages: 1, wiki: document.wikiState })) }),
    searchSelectedSources: async (caller, selection, query, limit) => selection.documents.slice(0, limit)
      .map(item => ({ id: item.documentId + ':0', kbId: item.kbId, documentId: item.documentId,
        title: item.name, originalRevision: item.originalRevision, extractionRevision: item.extractionRevision,
        coverage: item.coverage, location: { kind: item.location, number: 1 }, text, score: 1, url: '/provider/unused' })),
    readSelectedSources: async (caller, selection, documentId) => ({ version: 1,
      document: selection.documents.find(item => item.documentId === documentId),
      pages: [{ number: 1, text, url: '/provider/page' }], truncated: false }),
    original: async () => ({ document: { ...document }, bytes: Buffer.from(text) }),
  };
}
async function workbench() {
  let sourceSha = '';
  const works = new Map();
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    const send = value => { response.writeHead(200, { 'content-type': 'application/json' }); response.end(JSON.stringify(value)); };
    const api = '/api/projects/p/sor-documents/d/agent-structure/delivery/plugin-workbench';
    if (request.method === 'POST') {
      let body = ''; for await (const chunk of request) body += chunk;
      const input = JSON.parse(body || '{}');
      const sessionId = input.session_id || url.pathname.slice(api.length + 1);
      if (!works.has(sessionId)) works.set(sessionId, { session_id: sessionId, source_sha256: input.expected_source_sha256 || sourceSha,
        project_id: 'p', document_id: 'd', revision_id: null, revision_number: 0,
        require_map_confirmation: input.require_map_confirmation === true, confirmed_map_sha256: null,
        stages: [], versions: [], feedback: [] });
      const value = works.get(sessionId);
      if (input.artifacts) { value.revision_id = sha(JSON.stringify(input.artifacts)); value.revision_number += 1; }
      send(value); return;
    }
    const sessionId = url.pathname.startsWith(api + '/') ? url.pathname.slice(api.length + 1) : '';
    const value = works.get(sessionId);
    if (value) { send(value); return; }
    response.writeHead(404); response.end();
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { origin: 'http://127.0.0.1:' + server.address().port,
    setSourceSha: value => { sourceSha = value; },
    close: () => new Promise(ok => server.close(ok)) };
}
function runtimeFor(dataRoot, cwd) {
  return { dataRoot, attestationKey: 'a'.repeat(64), extraEnv: {}, async command(argv, env = {}, options = {}) {
    const childEnv = { ...helperEnv() };
    for (const [name, value] of Object.entries(env)) { if (value === undefined) delete childEnv[name]; else childEnv[name] = value; }
    Object.assign(childEnv, this.extraEnv);
    const result = await run(argv, { cwd, env: childEnv, signal: options.signal });
    if (result.code !== 0) throw Error('Runtime command failed: ' + result.stderr.slice(-2000) + result.stdout.slice(-2000));
    return result.stdout;
  } };
}
async function prepare({ bundlePath, commandId, root, bridge, binding, adopted, workbenchOrigin, cacheRoot }) {
  const bundleSha = JSON.parse(await readFile(join(bundlePath, 'build-receipt.json'), 'utf8')).bundle_sha256;
  const workspace = join(root, 'workspace');
  const applicationId = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc';
  const source = join(fixture, 'example.pdf');
  const sourceSha = sha(await readFile(source));
  const requestFile = join(root, 'prepare.json');
  const workbenchUrl = workbenchOrigin + '/workspace?projectId=p&documentId=d';
  await writeFile(requestFile, JSON.stringify({ workspace, expected_source_sha256: sourceSha,
    bundle_sha256: bundleSha, application_id: applicationId,
    knowledge_evidence: binding, evidence_mode: binding.mode,
    evidence_reason: binding.mode === 'NONE' ? 'NO_INTERNAL_EVIDENCE' : 'HOST_SELECTED_TASK_EVIDENCE',
    request: { mode: 'create', source, supplementary_instructions: 'Use the selected fixture evidence only.',
      creation_key: commandId, project_key: 'project-p', document_key: 'document-d',
      workbench_url: workbenchUrl, require_map_confirmation: false } }));
  const prepared = JSON.parse(await command([python, join(bundlePath, 'scripts/host_launch.py'), requestFile],
    { cwd: root, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
  assert.equal(prepared.source_sha256, sourceSha);
  assert.equal(prepared.bundle_sha256, bundleSha);
  assert.match(prepared.work_ref || '', /^plugin-[a-f0-9]{32}$/);
  const taskRoot = prepared.task_root;
  const taskContext = JSON.parse(await readFile(join(taskRoot, 'task-context.json'), 'utf8'));
  const mapInput = JSON.parse(await readFile(join(fixture, 'map/map-decisions.json'), 'utf8'));
  const documentNode = mapInput.nodes.find(node => node.kind === 'DOCUMENT');
  if (documentNode) documentNode.title = basename(taskContext.source.path);
  const mapInputPath = join(root, 'map-decisions.json');
  await writeFile(mapInputPath, JSON.stringify(mapInput));
  const map = JSON.parse(await command([python, join(bundlePath, 'scripts/reqpilot.py'), 'workspace', 'run', 'compile-map',
    '--workspace', taskRoot, '--source-ir', join(fixture, 'capture/source_ir.json'),
    '--input', mapInputPath],
    { cwd: workspace, env: { PYTHONDONTWRITEBYTECODE: '1', REQPILOT_WORKSPACE_DEFER_SYNC: '1' } }));
  const mapDir = map.selected.MAP.output_dir;
  const compiledMap = JSON.parse(await readFile(join(mapDir, 'document-map.json'), 'utf8'));
  const itemInput = JSON.parse(await readFile(join(fixture, 'item-decisions.json'), 'utf8'));
  itemInput.document_map_artifact_sha256 = compiledMap.artifact_sha256;
  const itemInputPath = join(root, 'item-decisions.json');
  await writeFile(itemInputPath, JSON.stringify(itemInput));
  const items = JSON.parse(await command([python, join(bundlePath, 'scripts/reqpilot.py'), 'workspace', 'run', 'compile-items',
    '--workspace', taskRoot, '--map-dir', mapDir, '--input', itemInputPath],
    { cwd: workspace, env: { PYTHONDONTWRITEBYTECODE: '1', REQPILOT_WORKSPACE_DEFER_SYNC: '1' } }));
  const inspectFile = join(root, 'inspect.json');
  await writeFile(inspectFile, JSON.stringify({ task_id: prepared.task_id }));
  const inspected = JSON.parse(await command([python, join(bundlePath, 'scripts/reqpilot.py'), 'task', 'inspect',
    '--working-root', prepared.workspace, '--input', inspectFile], { cwd: workspace, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
  const baseline = JSON.parse(await readFile(inspected.selected.ITEMS.path, 'utf8'));
  const unitId = baseline.records[0].source_record_id;
  const workflow = prepared.workflow?.workflow;
  assert.ok(workflow);
  const record = { commandId, projectId: 'p', documentId: 'd', sourceSha256: sourceSha, bundleSha,
    python, workflow, prepared: { ...prepared, workspace, source_sha256: sourceSha, bundle_sha256: bundleSha } };
  bridge.registerRecord(record);
  return { prepared, taskRoot, workspace, sourceSha, bundleSha, unitId,
    unitIds: baseline.records.map(record => record.source_record_id), inspected, mapDir, items, record };
}
async function helperCall({ root, workspace, cacheRoot, payload, request, attestationKey }) {
  const requestFile = join(root, 'helper-request-' + randomUUID() + '.json');
  const payloadFile = join(root, 'helper-command-' + randomUUID() + '.json');
  if (request !== undefined) await writeFile(requestFile, JSON.stringify(request), { mode: 0o600 });
  if (request !== undefined) payload.request_file = requestFile;
  await writeFile(payloadFile, JSON.stringify(payload), { mode: 0o600 });
  const cwd = await mkdtemp(join(tmpdir(), 'reqpilot-host-command-cwd-'));
  try {
    const result = await run([python, '-I', helper, payloadFile, workspace, cacheRoot], { cwd,
      env: attestationKey ? { REQPILOT_HOST_ATTESTATION_KEY: attestationKey } : {} });
    let value = null; try { value = JSON.parse(result.stdout); } catch {}
    return { ...result, value };
  } finally { await rm(cwd, { recursive: true, force: true }); }
}

test('direct -I entry reproduces the original missing sibling import baseline', { skip: !enabled, timeout: 60000 }, async () => {
  await mkdir(workRoot, { recursive: true });
  const root = await mkdtemp(join(workRoot, 'baseline-'));
  const cwd = await mkdtemp(join(tmpdir(), 'reqpilot-host-command-cwd-'));
  try {
    const request = join(root, 'request.json'); await writeFile(request, JSON.stringify({ task_id: 'task-probe' }));
    const result = await run([python, '-I', join(bundle, 'scripts/reqpilot.py'), 'task', 'evidence_status',
      '--working-root', root, '--input', request], { cwd });
    assert.notEqual(result.code, 0);
    assert.match(result.stderr + result.stdout, /No module named 'task_service'/);
  } finally { await rm(root, { recursive: true, force: true }); await rm(cwd, { recursive: true, force: true }); }
});

test('adopted analysis and evidence status execute through the isolated pinned Host command', { skip: !enabled, timeout: 300000 }, async () => {
  await mkdir(workRoot, { recursive: true });
  const root = await mkdtemp(join(workRoot, 'current-'));
  const commandId = randomUUID();
  const dataRoot = join(root, 'host-data'); await mkdir(dataRoot, { recursive: true });
  const cacheRoot = join(root, 'host-cache');
  const cwd = await mkdtemp(join(tmpdir(), 'reqpilot-host-command-cwd-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  const work = await workbench();
  try {
    const binding = await bridge.prepareAdoptedBinding({ commandId, projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    bridge.runtime = runtimeFor(dataRoot, cwd);
    const state = await prepare({ bundlePath: bundle, commandId, root, bridge, binding, adopted: service,
      workbenchOrigin: work.origin, cacheRoot });
    await bridge.adoptedEvidence(commandId, 'valve', 1);
    const evidence = await bridge.adoptedEvidence(commandId, 'valve', 1);
    assert.equal(evidence.facts.length, 1);
    const fact = evidence.facts[0];
    const decision = { unit_id: state.unitId, decision: 'PENDING_EVIDENCE',
      reason: 'The adopted fixture is contextual only; a verified product record is still required.',
      evidence: [{ fact_id: fact.fact_id, quote: fact.text }],
      clarification_question: 'Provide the controlled product record for the fixture response condition.' };
    const poison = join(root, 'hostile-sentinel');
    const hostile = join(root, 'hostile-pythonpath'); await mkdir(hostile, { recursive: true });
    const poisonCode = 'from pathlib import Path\nPath(' + JSON.stringify(poison) + ").write_text('executed')\n";
    await writeFile(join(hostile, 'sitecustomize.py'), poisonCode);
    await writeFile(join(state.taskRoot, 'sitecustomize.py'), poisonCode);
    const childReceipt = join(root, 'child-flags.json');
    bridge.runtime.extraEnv = { PYTHONPATH: hostile, REQPILOT_HOST_CHILD_RECEIPT: childReceipt };
    const revision = state.inspected.revision;
    const result = await bridge.analysis(commandId, [decision], [state.unitId], revision, 'analysis-one');
    assert.notEqual(result.status, 'FAILED', JSON.stringify(result));
    assert.equal(result.task_id, state.prepared.task_id);
    assert.equal(result.human_acceptance, 'NOT_CLAIMED');
    const child = JSON.parse(await readFile(childReceipt, 'utf8'));
    assert.equal(child.isolated, true);
    assert.equal(child.ignore_environment, true);
    assert.equal(child.argv[0], child.entry);
    await assert.rejects(readFile(poison), /ENOENT/);
    const context = JSON.parse(await readFile(state.taskRoot + '/task-context.json', 'utf8'));
    assert.equal(context.work_id, state.prepared.task_id);
    assert.equal(context.runtime_ref.bundle_sha256, state.bundleSha);
    const inspectFile = join(root, 'inspect.json');
    await writeFile(inspectFile, JSON.stringify({ task_id: state.prepared.task_id }));
    const inspect = async () => JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'task', 'inspect',
      '--working-root', state.workspace, '--input', inspectFile],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
    const firstAfter = await inspect();
    const replay = await bridge.analysis(commandId, [decision], [state.unitId], revision, 'analysis-one');
    assert.equal(replay.status, 'ANALYSIS_ALREADY_SAVED');
    assert.equal(replay.analysis?.artifact_sha256, result.analysis?.artifact_sha256);
    assert.equal((await inspect()).revision, firstAfter.revision);
    const historyPath = join(dataRoot, 'knowledge-analysis', commandId + '.history.json');
    const firstHistory = JSON.parse(await readFile(historyPath, 'utf8'));
    assert.equal(firstHistory.entries.length, 1);
    await writeFile(historyPath, JSON.stringify({ ...firstHistory, entries: [] }));
    const recovered = await bridge.analysis(commandId, [decision], [state.unitId], revision, 'analysis-one');
    assert.equal(recovered.status, 'ANALYSIS_ALREADY_SAVED');
    const recoveredHistory = JSON.parse(await readFile(historyPath, 'utf8'));
    assert.equal(recoveredHistory.entries.length, 1);
    assert.equal((await inspect()).revision, firstAfter.revision);
    assert.equal((await bridge.markStatus(commandId, 'CURRENT', 'first-status')), true);
    assert.equal((await bridge.markStatus(commandId, 'CURRENT', 'second-status')), true);
    const status = JSON.parse(await readFile(join(state.taskRoot, 'knowledge-evidence-status.json'), 'utf8'));
    assert.equal(status.task_id, state.prepared.task_id);
    assert.equal(status.state, 'CURRENT');
    const statusRequests = (await readdir(dataRoot)).filter(name => name.startsWith('knowledge-status-' + commandId + '-'));
    assert.equal(statusRequests.length, 2);
    assert.equal(new Set(statusRequests).size, 2);
    const history = JSON.parse(await readFile(historyPath, 'utf8'));
    assert.equal(history.task_id, state.prepared.task_id);
    assert.equal(history.entries.length, 1);
    const current = await inspect();
    assert.equal(current.task_id, state.prepared.task_id);
    const checkFile = join(root, 'check.json'); await writeFile(checkFile, JSON.stringify({ task_id: state.prepared.task_id }));
    const checked = JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'task', 'check',
      '--working-root', state.workspace, '--input', checkFile],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
    assert.notEqual(checked.status, 'FAILED', JSON.stringify(checked));
    const delivered = JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'workspace', 'run', 'deliver',
      '--workspace', state.taskRoot],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1', REQPILOT_WORKSPACE_DEFER_SYNC: '1' } }));
    assert.ok(delivered.selected.DELIVERY?.output_dir);
    await readFile(join(delivered.selected.DELIVERY.output_dir, 'matrix.xlsx'));
    assert.equal(delivered.task_root, state.taskRoot);
    const latestFile = join(root, 'latest.json'); await writeFile(latestFile, JSON.stringify({ task_id: state.prepared.task_id }));
    let latest = JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'task', 'inspect',
      '--working-root', state.workspace, '--input', latestFile],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
    const priorVersions = await readdir(join(state.taskRoot, 'review/reviews'));
    const priorDeliveries = await readdir(join(state.taskRoot, 'review/deliveries'));
    const priorInitial = await readFile(join(state.taskRoot, 'review/versions/initial/source-item-assessments.json'), 'utf8');
    const secondUnit = state.unitIds.find(unitId => unitId !== state.unitId);
    assert.ok(secondUnit);
    const secondDecision = { unit_id: secondUnit, decision: 'PENDING_EVIDENCE',
      reason: 'The existing review keeps the earlier assessment; this scope also needs a controlled record.',
      evidence: [{ fact_id: fact.fact_id, quote: fact.text }],
      clarification_question: 'Provide the controlled product record for the second fixture scope.' };
    const updated = await bridge.analysis(commandId, [secondDecision], [secondUnit], latest.revision, 'analysis-two');
    assert.notEqual(updated.status, 'FAILED', JSON.stringify(updated));
    assert.equal(updated.task_id, state.prepared.task_id);
    const afterUpdate = await inspect();
    assert.equal(afterUpdate.task_id, state.prepared.task_id);
    assert.notEqual(afterUpdate.revision, latest.revision);
    assert.deepEqual((await readdir(join(state.taskRoot, 'review/reviews'))).filter(name => priorVersions.includes(name))
      .sort(), priorVersions.sort());
    assert.equal(await readFile(join(state.taskRoot, 'review/versions/initial/source-item-assessments.json'), 'utf8'), priorInitial);
    const analysisRequests = (await readdir(join(dataRoot, 'knowledge-analysis')))
      .filter(name => name.startsWith(commandId + '-analysis-') && name.endsWith('.json'));
    assert.equal(analysisRequests.length, 4);
    assert.equal(new Set(analysisRequests).size, 4);
    const requestCommands = [];
    for (const name of analysisRequests) requestCommands.push(JSON.parse(await readFile(join(dataRoot, 'knowledge-analysis', name), 'utf8')).command_id);
    assert.deepEqual(requestCommands.sort(), ['analysis-one', 'analysis-one', 'analysis-one', 'analysis-two']);
    const secondDelivery = JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'workspace', 'run', 'deliver',
      '--workspace', state.taskRoot],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1', REQPILOT_WORKSPACE_DEFER_SYNC: '1' } }));
    assert.ok(secondDelivery.selected.DELIVERY?.output_dir);
    await readFile(join(secondDelivery.selected.DELIVERY.output_dir, 'matrix.xlsx'));
    const deliveriesAfter = await readdir(join(state.taskRoot, 'review/deliveries'));
    assert.ok(priorDeliveries.every(name => deliveriesAfter.includes(name)));
    latest = await inspect();
    const staleError = await bridge.analysis(commandId, [decision], [state.unitId], 'f'.repeat(64), 'analysis-stale')
      .then(() => null, error => error);
    assert.match(String(staleError?.message || ''), /REVISION|TASK_WORKFLOW/);
    assert.equal(String(staleError?.message || '').includes('a'.repeat(64)), false);
    const afterStale = JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'task', 'inspect',
      '--working-root', state.workspace, '--input', latestFile],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
    assert.equal(afterStale.revision, latest.revision);
    const basePayload = { schema: 'reqpilot-pair-host-command/v1', action: 'inspect',
      task_id: state.prepared.task_id, bundle_sha256: state.bundleSha, source_sha256: state.sourceSha };
    const foreignPackage = await helperCall({ root, workspace: state.workspace, cacheRoot,
      payload: { ...basePayload, bundle_sha256: 'f'.repeat(64) } });
    assert.match(foreignPackage.value?.error || '', /TASK_WORKFLOW_PACKAGE_MISMATCH/);
    const foreignTask = await helperCall({ root, workspace: state.workspace, cacheRoot,
      payload: { ...basePayload, task_id: 'task-foreign' } });
    assert.match(foreignTask.value?.error || '', /INVALID_TASK|TASK_WORKFLOW_TASK_MISMATCH/);
    const foreignWork = await helperCall({ root, workspace: state.workspace, cacheRoot,
      payload: { ...basePayload, action: 'evidence_status', project_id: 'p', document_id: 'd',
        work_ref: 'plugin-' + 'f'.repeat(32), expected_revision: latest.revision, workflow: state.record.workflow },
      request: { task_id: state.prepared.task_id, state: 'CURRENT', message: '', binding_sha256: '1'.repeat(64) } });
    assert.match(foreignWork.value?.error || '', /TASK_WORKFLOW_WORK_REF_MISMATCH/);
    const tampered = join(root, 'tampered-workspace');
    await cp(state.workspace, tampered, { recursive: true });
    const sentinel = join(root, 'tampered-executed');
    await writeFile(join(tampered, '.reqpilot/scripts/task_service.py'),
      '\nfrom pathlib import Path as _P\n_P(' + JSON.stringify(sentinel) + ").write_text('executed')\n", { flag: 'a' });
    const tamperedResult = await helperCall({ root, workspace: tampered, cacheRoot: join(root, 'tampered-cache'),
      payload: basePayload });
    assert.match(tamperedResult.value?.error || '', /TASK_WORKFLOW_PACKAGE_MISMATCH/);
    await assert.rejects(readFile(sentinel), /ENOENT/);
    const oldRequestFile = join(dataRoot, 'knowledge-analysis',
      (await readdir(join(dataRoot, 'knowledge-analysis'))).find(name => name.startsWith(commandId + '-analysis-')));
    const forged = JSON.parse(await readFile(oldRequestFile, 'utf8'));
    forged.expected_revision = latest.revision;
    forged.command_id = 'analysis-forged';
    forged.knowledge_attestation.task_revision = latest.revision;
    forged.knowledge_attestation.command_id = 'analysis-forged';
    forged.knowledge_attestation.signature = (forged.knowledge_attestation.signature[0] === '0' ? '1' : '0')
      + forged.knowledge_attestation.signature.slice(1);
    const forgedResult = await helperCall({ root, workspace: state.workspace, cacheRoot,
      payload: { ...basePayload, action: 'analysis', project_id: 'p', document_id: 'd',
        work_ref: state.prepared.work_ref, expected_revision: latest.revision, workflow: state.record.workflow },
      request: forged, attestationKey: 'a'.repeat(64) });
    assert.equal(forgedResult.value?.status, 'FAILED', JSON.stringify(forgedResult));
    assert.match(String(forgedResult.value?.error_code || forgedResult.value?.error || ''), /HOST_ATTESTATION_INVALID/, JSON.stringify(forgedResult));
    assert.equal((forgedResult.stdout + forgedResult.stderr).includes('a'.repeat(64)), false);
    const afterForged = JSON.parse(await command([python, join(bundle, 'scripts/reqpilot.py'), 'task', 'inspect',
      '--working-root', state.workspace, '--input', latestFile],
      { cwd: state.workspace, env: { PYTHONDONTWRITEBYTECODE: '1' } }));
    assert.equal(afterForged.revision, latest.revision);
  } finally { await bridge.close(); await work.close(); if (!process.env.PAIR_HOST_COMMAND_KEEP) await rm(root, { recursive: true, force: true }); await rm(cwd, { recursive: true, force: true }); }
});

test('an older frozen task package keeps evidence status and refuses unsupported analysis actionably', { skip: !enabled || !legacyBundle, timeout: 300000 }, async () => {
  await mkdir(workRoot, { recursive: true });
  const root = await mkdtemp(join(workRoot, 'legacy-'));
  const commandId = randomUUID();
  const dataRoot = join(root, 'host-data'); await mkdir(dataRoot, { recursive: true });
  const cacheRoot = join(root, 'host-cache');
  const cwd = await mkdtemp(join(tmpdir(), 'reqpilot-host-command-cwd-'));
  const service = fakeProject();
  const bridge = new KnowledgeBridge({ service, dataRoot, applicationId: service.applicationId });
  const work = await workbench();
  try {
    const binding = await bridge.prepareBinding({ commandId, projectId: 'p', mode: 'PROJECT_REFERENCES',
      projectDocumentIds: ['project-doc'] });
    bridge.runtime = runtimeFor(dataRoot, cwd);
    const state = await prepare({ bundlePath: legacyBundle, commandId, root, bridge, binding, adopted: service,
      workbenchOrigin: work.origin, cacheRoot });
    const evidence = await bridge.search(commandId, 'valve', 1);
    const fact = evidence.facts[0];
    const decision = { unit_id: state.unitId, decision: 'PENDING_EVIDENCE',
      reason: 'The older frozen task still needs a controlled product record.',
      evidence: [{ fact_id: fact.fact_id, quote: fact.text }],
      clarification_question: 'Provide the controlled product record for the older task.' };
    await assert.rejects(bridge.analysis(commandId, [decision], [state.unitId], state.inspected.revision, 'legacy-analysis'),
      /TASK_WORKFLOW_ACTION_UNAVAILABLE|TASK_COMMAND_FAILED/);
    assert.equal(await bridge.markStatus(commandId, 'CURRENT', ''), true);
    const status = JSON.parse(await readFile(join(state.taskRoot, 'knowledge-evidence-status.json'), 'utf8'));
    assert.equal(status.task_id, state.prepared.task_id);
    assert.equal(status.state, 'CURRENT');
    const context = JSON.parse(await readFile(join(state.taskRoot, 'task-context.json'), 'utf8'));
    assert.equal(context.runtime_ref.bundle_sha256, state.bundleSha);
    assert.notEqual(state.bundleSha, JSON.parse(await readFile(join(bundle, 'build-receipt.json'), 'utf8')).bundle_sha256);
  } finally { await bridge.close(); await work.close(); if (!process.env.PAIR_HOST_COMMAND_KEEP) await rm(root, { recursive: true, force: true }); await rm(cwd, { recursive: true, force: true }); }
});
