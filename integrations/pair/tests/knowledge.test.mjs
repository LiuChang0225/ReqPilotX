import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { KnowledgeBridge } from '../host/src/knowledge.js';
import { TaskLauncher } from '../host/src/task-launch.js';

const digest = value => createHash('sha256').update(Buffer.isBuffer(value) ? value : typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const revision = text => digest(text);

function fakeKnowledge() {
  const applicationId = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc';
  const projectBase = { id: 'kb-project', name: 'Project references', owner: { kind: 'application-project', applicationId, projectId: 'p' } };
  const teamBase = { id: 'kb-team', name: 'Team library' };
  const documents = new Map();
  const add = (kbId, id, name, text) => documents.set(id, { id, kbId, name, mime: 'text/plain', state: 'indexed',
    sha256: digest(text), backendRevision: revision(text + ':extract'), expectedPages: 1, pages: [{ number: 1, text }], wikiState: 'not-requested' });
  add('kb-project', 'project-doc', 'internal.txt', 'The measured standby current was 4 mA at 12 V and 25 C.');
  add('kb-project', 'unselected-doc', 'other.txt', 'Unselected text must not be returned.');
  add('kb-team', 'team-doc', 'team.txt', 'Team live evidence says the valve opens in 28 ms at 12 V and 23 C.');
  const selections = new Map(); const pins = new Map(); const calls = []; const control = { onOriginal: null };
  const bases = [projectBase, teamBase];
  const selectedDocs = caller => (selections.get(caller) || []).flatMap(kbId =>
    [...documents.values()].filter(document => document.kbId === kbId && document.state !== 'deleted'));
  const capture = async (caller, pairs) => {
    const allowed = new Set(selections.get(caller) || []);
    return { version: 1, caller, kbIds: [...allowed],
      documents: pairs.map(({ kbId, documentId }) => {
        if (!allowed.has(kbId)) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
        const document = documents.get(documentId);
        if (!document || document.kbId !== kbId) throw Object.assign(Error('document-unselected'), { code: 'document-unselected' });
        return { kbId, documentId, name: document.name, mime: document.mime,
          originalRevision: document.sha256, extractionRevision: document.backendRevision,
          coverage: 'complete', location: 'logical-section', pages: document.pages.length,
          expectedPages: document.expectedPages, wiki: document.wikiState };
      }) };
  };
  return {
    applicationId, calls, documents, selections, pins, control,
    snapshot: () => ({ schemaVersion: 1, bases: bases.map(base => ({ ...base })), documents: [...documents.values()].map(value => ({ ...value })), jobs: [], selections: {}, wikiEdits: [] }),
    getProjectReferenceLibrary: owner => owner.projectId === 'p' ? bases.find(base => base.id === 'kb-project') : undefined,
    ensureProjectReferenceLibrary: async () => bases.find(base => base.id === 'kb-project'),
    select: async (caller, ids) => { selections.set(caller, [...ids]); calls.push({ op: 'select', caller, ids }); },
    captureSelectedSources: async (caller, pairs) => { calls.push({ op: 'capture', caller, pairs }); return capture(caller, pairs); },
    searchSelectedSources: async (caller, selection, query, limit) => {
      if (pins.has(caller)) throw Object.assign(Error('pinned-topic-required'), { code: 'pinned-topic-required' });
      calls.push({ op: 'search', caller, query });
      return selection.documents.filter(document => document.name !== 'unselected-doc')
        .map((document, index) => ({ id: document.documentId + ':' + index, kbId: document.kbId,
          documentId: document.documentId, title: document.name, originalRevision: document.originalRevision,
          extractionRevision: document.extractionRevision, coverage: document.coverage,
          location: { kind: document.location, number: 1 },
          text: documents.get(document.documentId).pages[0].text, score: 1, url: '/provider/unused' }))
        .slice(0, limit);
    },
    readSelectedSources: async (caller, selection, documentId, firstPage, count) => {
      if (pins.has(caller)) throw Object.assign(Error('pinned-topic-required'), { code: 'pinned-topic-required' });
      const document = selection.documents.find(item => item.documentId === documentId);
      if (!document) throw Object.assign(Error('document-unselected'), { code: 'document-unselected' });
      const record = documents.get(documentId);
      return { version: 1, document, pages: record.pages.map(page => ({ ...page, url: '/provider/page' })), truncated: false };
    },
    importProjectReferenceSource: async (kbId, name, bytes) => {
      const text = Buffer.from(bytes).toString(); const id = 'imported-' + digest(text).slice(0, 8);
      const document = { id, kbId, name, mime: 'text/plain', sha256: digest(text), backendRevision: revision(text), expectedPages: 1, pages: [{ number: 1, text }], state: 'indexed', wikiState: 'not-requested' };
      documents.set(id, document);
      return { document, job: { id: 'job-' + id, documentId: id, state: 'source-ready', message: '', updatedAt: '', model: null }, sourceReady: true, wiki: 'not-requested' };
    },
    original: async id => { const document = documents.get(id); if (!document) throw Error('document-unavailable');
      const before = { document: { ...document, pages: document.pages.map(page => ({ ...page })) }, bytes: Buffer.from(document.pages[0].text) };
      if (typeof control.onOriginal === 'function') control.onOriginal(document);
      return before; },
    topicVersions: async (caller, kbId) => [{ kbId: 'kb-team', versionId: 'topic-v1', topicId: 'topic-id',
      title: 'Retained design topic', availability: { state: 'available', acceptedBy: 'curator' },
      verification: { state: 'verified', confirmedBy: 'reviewer' }, origin: 'human-authored',
      applicability: { state: 'known', description: 'Team design applicability' }, authors: ['engineer'] }],
    topicSelection: caller => pins.get(caller),
    selectTopicVersion: async (caller, kbId, versionId) => { calls.push({ op: 'selectTopicVersion', caller, kbId, versionId });
      if (versionId === null) pins.delete(caller); else pins.set(caller, { kbId, versionId }); },
    readTopicVersion: async (caller, kbId, versionId) => {
      if (pins.get(caller)?.versionId !== versionId) throw Object.assign(Error('pinned-topic-required'), { code: 'pinned-topic-required' });
      return { formatVersion: 1, versionId, kbId, topicId: 'topic-id',
        availability: { state: 'available', acceptedBy: 'curator' },
        verification: { state: 'verified', confirmedBy: 'reviewer' }, origin: 'human-authored',
        topic: { title: 'Retained design topic', applicability: { state: 'known', description: 'Team design applicability' },
          content: 'Derived topic content stays separate from source citations.', revision: 'c'.repeat(64) },
        authors: ['engineer'], sources: [{ documentId: '11111111-1111-4111-8111-111111111111', title: 'design.md',
          originalRevision: 'd'.repeat(64), extractionRevision: 'e'.repeat(64), coverage: 'complete',
          location: { kind: 'physical-page', number: 2 },
          text: 'The retained team topic states the valve opens in 28 ms at 12 V and 23 C.',
          url: '/api/pair-knowledge/topic/source/kb-team/topic-v1/11111111-1111-4111-8111-111111111111' }] };
    },
    topicOriginal: async (kbId, versionId, documentId) => { calls.push({ op: 'topicOriginal', kbId, versionId, documentId });
      return { document: { name: 'design.md', mime: 'text/markdown', sha256: 'd'.repeat(64) }, bytes: Buffer.from('topic original') }; },
    close: async () => {},
  };
}

test('project, team-topic and combined bindings use isolated scopes and consumed tool facts', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-'));
  const service = fakeKnowledge();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
  try {
    const inventory = await bridge.inventory('p');
    assert.equal(inventory.projectLibrary.id, 'kb-project');
    assert.deepEqual(inventory.teamLibraries.map(value => value.id), ['kb-team']);
    assert.equal(inventory.projectDocuments.length, 2);
    const binding = await bridge.prepareBinding({ commandId: 'command-one', projectId: 'p', mode: 'PROJECT_REFERENCES', projectDocumentIds: ['project-doc'] });
    assert.equal(binding.mode, 'PROJECT_REFERENCES');
    assert.deepEqual(binding.project.documents.map(value => value.documentId), ['project-doc']);
    assert.equal(service.calls.filter(value => value.op === 'capture').length, 1);
    assert.ok(service.calls.some(value => value.op === 'select' && value.caller.endsWith(':project')));
    const taskRoot = join(root, 'task'); await mkdir(taskRoot, { recursive: true });
    bridge.registerRecord({ commandId: 'command-one', prepared: { task_root: taskRoot,
      workspace: taskRoot, task_id: 'task-one' }, workspace: taskRoot });
    const search = await bridge.search('command-one', 'standby', 5);
    assert.equal(search.facts.length, 1);
    assert.equal(search.facts[0].document_id, 'project-doc');
    assert.equal(search.facts[0].source_ref.kind, 'logical-section');
    assert.equal(search.facts[0].source_ref.location.page_number, null);
    assert.equal(search.facts[0].source_ref.location.section, 1);
    const read = await bridge.read('command-one', 'project-doc', 1, 1);
    assert.equal(read.facts[0].source_ref.kind, 'logical-section');
    assert.equal(read.facts[0].source_ref.location.page_number, null);
    assert.equal(read.facts[0].source_ref.location.section, 1);
    const { catalog, registration } = await bridge.writeCatalog('command-one');
    assert.equal(catalog.documents.length, 1);
    assert.equal(Object.keys(catalog.evidence_catalog).length, 1);
    assert.equal(registration.binding_sha256, binding.binding_sha256);
    assert.equal(registration.receipts.length, 2);
    assert.deepEqual((await readFile(join(taskRoot, 'inputs/knowledge/knowledge-catalog.json'), 'utf8')).includes('pair-fact-'), true);
    const restarted = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
    restarted.registerRecord({ commandId: 'command-one', prepared: { task_root: taskRoot, workspace: taskRoot, task_id: 'task-one' } });
    try {
      assert.equal((await restarted.loadBinding('command-one')).binding_sha256, binding.binding_sha256);
      assert.equal((await restarted.checkBinding('command-one')).state, 'CURRENT');
    } finally { await restarted.close(); }

    const combined = await bridge.prepareBinding({ commandId: 'command-two', projectId: 'p', mode: 'COMBINED',
      projectDocumentIds: ['project-doc'], team: { kbId: 'kb-team', topicVersionId: 'topic-v1', authoritative: true } });
    assert.equal(combined.team.kind, 'TOPIC');
    assert.ok(service.pins.has(combined.team.caller));
    assert.notEqual(combined.project.caller, combined.team.caller);
    assert.deepEqual((service.selections.get(combined.project.caller) || []), ['kb-project']);
    assert.deepEqual((service.selections.get(combined.team.caller) || []), ['kb-team']);
    bridge.registerRecord({ commandId: 'command-two', prepared: { task_root: taskRoot, workspace: taskRoot, task_id: 'task-two' } });
    const mixed = await bridge.search('command-two', 'valve', 10);
    assert.ok(mixed.facts.some(fact => fact.document_id === 'project-doc'));
    assert.ok(mixed.facts.some(fact => fact.origin === 'PAIR_RETAINED_TOPIC'
      && fact.document_id === '11111111-1111-4111-8111-111111111111'
      && fact.source_ref.kind === 'physical-page'
      && fact.source_ref.location.number === 2
      && fact.source_ref.location.page_number === 2
      && fact.source_ref.location.section === null));
    assert.equal(service.calls.filter(value => value.op === 'search' && value.caller === combined.team.caller).length, 0);
    assert.equal((await bridge.checkBinding('command-two')).state, 'CURRENT');
    const topic = await bridge.topicRead('command-two');
    assert.equal(topic.topic.content, 'Derived topic content stays separate from source citations.');
    assert.equal(topic.sources[0].location.number, 2);
    assert.ok(topic.facts.every(fact => fact.origin === 'PAIR_RETAINED_TOPIC'));
    assert.equal(topic.facts[0].document_id, topic.sources[0].documentId);
    assert.equal(topic.facts[0].original_revision, topic.sources[0].originalRevision);
    assert.equal(service.calls.some(value => value.op === 'search' && value.caller === combined.team.caller), false);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('team selection rejects another application-project base and changed versions refuse without retargeting', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-'));
  const service = fakeKnowledge();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
  try {
    await assert.rejects(bridge.prepareBinding({ commandId: 'bad', projectId: 'p', mode: 'TEAM_KNOWLEDGE',
      team: { kbId: 'kb-project', documents: ['project-doc'] } }), /TEAM_LIBRARY_INVALID/);
    await bridge.prepareBinding({ commandId: 'stable', projectId: 'p', mode: 'TEAM_KNOWLEDGE',
      team: { kbId: 'kb-team', documents: ['team-doc'] } });
    service.documents.get('team-doc').backendRevision = 'c'.repeat(64);
    assert.equal((await bridge.checkBinding('stable')).state, 'CHANGED');
    const statuses = [];
    bridge.getRecord = () => ({ commandId: 'stable', prepared: { task_root: join(root, 'task'), workspace: join(root, 'task'), task_id: 'task-stable' }, capabilityRoot: join(root, 'bundle') });
    bridge.runtime = { dataRoot: root, command: async (argv, env, options) => { statuses.push(argv); return '{"status":"EVIDENCE_STATUS_SAVED"}'; } };
    await mkdir(join(root, 'task'), { recursive: true });
    await assert.rejects(bridge.search('stable', 'valve', 5), /KNOWLEDGE_EVIDENCE_CHANGED/);
    assert.equal(statuses.length, 1);
    assert.ok(statuses[0].includes('evidence_status'));
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('TaskLauncher freezes the Host binding before preparation and exposes both task and evidence MCP descriptors', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-launch-'));
  const source = Buffer.from('original'); const sha = digest(source);
  const commandId = '12345678-1234-4234-8234-123456789abc';
  const prepared = [];
  const runtime = { dataRoot: root, python: '/installed/python', ownerId: commandId,
    snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:1234' },
    async command(args, env, options) {
      if (args.some(value => String(value).endsWith('task-workflow.py'))) return JSON.stringify({ status: 'READY', task_id: 'task-one',
        workflow: { schema: 'reqpilot-fixed-workflow-state/v1', source_sha256: sha,
          workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0', definition_sha256: 'd'.repeat(64),
            brief_sha256: 'f'.repeat(64), evidence_mode: 'PROJECT_REFERENCES' },
          stages: [{ id: 'PREPARATION', label: '资料准备', status: 'SAVED', evidence: [{ kind: 'TASK_CONTEXT', source_sha256: sha }] }],
          next_stage: 'SOURCE_MAP', ready_to_review: false, verification: 'SAVED_ARTIFACTS_ONLY', open_items: [], human_acceptance: 'NOT_CLAIMED' } });
      const input = JSON.parse(await readFile(args[2], 'utf8')); prepared.push(input);
      await mkdir(input.workspace, { recursive: true });
      const workspace = input.workspace;
      return JSON.stringify({ schema: 'reqpilot-host-launch/v1', workspace, task_id: 'task-one', task_root: workspace,
        work_ref: 'plugin-' + 'a'.repeat(32), source_sha256: sha, bundle_sha256: 'b'.repeat(64), prompt: 'Read request.md',
        workflow: { schema: 'reqpilot-fixed-workflow-state/v1', source_sha256: sha,
          workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0', definition_sha256: 'd'.repeat(64),
            brief_sha256: 'f'.repeat(64), evidence_mode: 'PROJECT_REFERENCES' }, stages: [], ready_to_review: false,
          verification: 'SAVED_ARTIFACTS_ONLY', open_items: [], human_acceptance: 'NOT_CLAIMED' },
        tools: { schema: 'reqpilot-host-tools/v1', mcp: { command: '/installed/python',
          args: [join(workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', workspace, '--source-root', workspace], cwd: workspace } } });
    } };
  const knowledge = {
    available: () => true,
    prepareBinding: async input => ({ schema_version: 'reqpilot-task-evidence/v1', mode: input.mode,
      application_id: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc', project_id: 'p',
      created_at: '2026-10-31T00:00:00Z', project: { library_id: 'kb-project', caller: 'task:project',
        documents: [{ kbId: 'kb-project', documentId: 'project-doc', name: 'internal.txt', mime: 'text/plain',
          originalRevision: '1'.repeat(64), extractionRevision: '2'.repeat(64), coverage: 'complete',
          location: 'logical-section', pages: 1, expectedPages: 1, wiki: 'not-requested' }], roles: {} },
      team: null, binding_sha256: 'c'.repeat(64) }),
    bindingPath: id => join(root, 'knowledge-bindings', id + '.binding.json'),
    registerRecord() {}, checkBinding: async () => ({ state: 'CURRENT', message: '' }),
    mcpBinding: async (id, record) => ({ command: record.python, args: ['/knowledge-mcp.py', '/runtime.json'], cwd: record.prepared.workspace, serverName: 'reqpilot_knowledge' }),
  };
  const services = { defaultModel: { currentSelection: () => ({ provider: 'p', model: 'm' }) },
    workspaces: { async create() { return { workspace: { workspaceId: 'workspace-one' } }; } },
    sessions: { async create(value) { return { sessionId: value.sessionId }; },
      async selectSessionModel({ sessionId, ...selected }) { return { selected }; },
      async prompt() { return { accepted: true }; } } };
  const transport = async url => url.endsWith('/file') ? new Response(source)
    : new Response(JSON.stringify([{ id: 'd', file_name: 'source.pdf', source_sha256: sha }]));
  const launcher = new TaskLauncher(runtime, { taskMcp: true }, () => services, transport, knowledge);
  launcher.ready = true; launcher.capabilityRoot = root; launcher.bundleSha = 'b'.repeat(64);
  launcher.workflow = { schema: 'reqpilot-fixed-workflow/v1', id: 'reqpilot-fixed-workflow', version: '1.0.0',
    definition_sha256: 'd'.repeat(64), evidence: { mode: 'NONE', reason: 'INTERNAL_EVIDENCE_NOT_WIRED' } };
  try {
    const receipt = await launcher.start({ commandId, projectId: 'p', documentId: 'd', sourceSha256: sha,
      goal: 'Use only the selected project evidence.', knowledge: { mode: 'PROJECT_REFERENCES', projectDocumentIds: ['project-doc'] } });
    assert.equal(receipt.status, 'ACCEPTED');
    assert.equal(receipt.knowledgeEvidence.mode, 'PROJECT_REFERENCES');
    assert.equal(prepared.length, 1);
    assert.equal(prepared[0].evidence_mode, 'PROJECT_REFERENCES');
    assert.equal(prepared[0].knowledge_evidence.binding_sha256, 'c'.repeat(64));
    const saved = JSON.parse(await readFile(join(root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(saved.workflow.evidence.mode, 'PROJECT_REFERENCES');
    const bindings = await launcher.toolBindings(receipt.sessionId);
    assert.equal(bindings.length, 2);
    assert.equal(bindings[0].serverName, undefined);
    assert.equal(bindings[1].serverName, 'reqpilot_knowledge');
  } finally { await launcher.close(); await rm(root, { recursive: true, force: true }); }
});

test('analysis transport writes the consumed catalog and invokes the typed task owner without a second model', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-analysis-'));
  const service = fakeKnowledge();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
  try {
    await bridge.prepareBinding({ commandId: 'analysis-command', projectId: 'p', mode: 'PROJECT_REFERENCES', projectDocumentIds: ['project-doc'] });
    const taskRoot = join(root, 'task'); const bundle = join(root, 'bundle');
    await mkdir(taskRoot, { recursive: true }); await mkdir(join(bundle, 'scripts'), { recursive: true });
    bridge.registerRecord({ commandId: 'analysis-command', python: '/installed/python', capabilityRoot: bundle,
      prepared: { task_root: taskRoot, workspace: taskRoot, task_id: 'task-analysis' } });
    const calls = [];
    bridge.runtime = { dataRoot: root, attestationKey: 'f'.repeat(64), async command(argv, env) {
      calls.push({ argv, env });
      assert.equal(env.REQPILOT_HOST_ATTESTATION_KEY, 'f'.repeat(64));
      const request = JSON.parse(await readFile(argv[argv.indexOf('--input') + 1], 'utf8'));
      assert.equal(request.expected_revision, request.command_id === 'model-command-two'
        ? 'workspace-revision-two' : 'workspace-revision-one');
      assert.equal(request.command_id, request.decisions[0].unit_id === 'u2' ? 'model-command-two' : 'model-command-one');
      assert.equal(request.knowledge_attestation.task_revision, request.expected_revision);
      assert.equal(request.knowledge_attestation.command_id, request.command_id);
      assert.equal(request.knowledge_attestation.signature.length, 64);
      return JSON.stringify({ status: 'WORKSPACE_OPERATION_SAVED', operation_state: 'COMMITTED',
        analysis: { artifact_sha256: 'a'.repeat(64), assessed_unit_count: 1, human_acceptance: 'NOT_CLAIMED' } });
    } };
    const search = await bridge.search('analysis-command', 'standby', 5);
    const fact = search.facts[0];
    await assert.rejects(bridge.analysis('analysis-command', [{ unit_id: 'u1', decision: 'COMPLIANT',
      reason: 'The selected internal result covers the source condition.',
      evidence: [{ fact_id: 'not-consumed', quote: 'invented' }], clarification_question: '' }],
      ['u1'], 'workspace-revision-one', 'model-command-one'), /EVIDENCE_FACT_NOT_CONSUMED/);
    assert.equal(calls.length, 0);
    const result = await bridge.analysis('analysis-command', [{ unit_id: 'u1', decision: 'COMPLIANT',
      reason: 'The selected internal result covers the source condition.',
      evidence: [{ fact_id: fact.fact_id, quote: fact.text }], clarification_question: '' }],
      ['u1'], 'workspace-revision-one', 'model-command-one');
    assert.equal(result.analysis.assessed_unit_count, 1);
    assert.equal(calls.length, 1);
    assert.ok(calls[0].argv.includes('analysis'));
    assert.ok(!calls[0].argv.includes('inspect'));
    const firstRequest = JSON.parse(await readFile(calls[0].argv[calls[0].argv.indexOf('--input') + 1], 'utf8'));
    assert.deepEqual(firstRequest.knowledge_attestation.decisions.map(item => item.unit_id), ['u1']);
    const second = await bridge.analysis('analysis-command', [{ unit_id: 'u2', decision: 'PENDING_EVIDENCE',
      reason: 'No suitable evidence for this unit.', evidence: [],
      clarification_question: 'Provide current evidence.' }], ['u2'], 'workspace-revision-two', 'model-command-two');
    assert.equal(second.analysis.assessed_unit_count, 1);
    assert.equal(calls.length, 2);
    const secondRequest = JSON.parse(await readFile(calls[1].argv[calls[1].argv.indexOf('--input') + 1], 'utf8'));
    assert.deepEqual(secondRequest.decisions.map(item => item.unit_id), ['u2']);
    assert.equal(secondRequest.knowledge_attestation.task_revision, 'workspace-revision-two');
    assert.deepEqual(secondRequest.knowledge_attestation.decisions.map(item => item.unit_id).sort(), ['u1', 'u2']);
    const history = JSON.parse(await readFile(join(root, 'knowledge-analysis', 'analysis-command.history.json'), 'utf8'));
    assert.equal(history.entries.length, 2);
    const catalog = JSON.parse(await readFile(join(taskRoot, 'inputs/knowledge/knowledge-catalog.json'), 'utf8'));
    assert.ok(catalog.evidence_catalog[fact.fact_id]);
    const registration = JSON.parse(await readFile(join(taskRoot, 'inputs/knowledge/evidence-registration.json'), 'utf8'));
    assert.equal(registration.receipts.length, 1);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

import { EventEmitter } from 'node:events';
function fakeServer({ fail = false, delay = 0 } = {}) {
  return () => {
    const server = new EventEmitter();
    server.listening = false; server.closed = false;
    server.listen = () => {
      if (fail) { queueMicrotask(() => server.emit('error', Error('listen failed'))); return; }
      setTimeout(() => { if (!server.closed) { server.listening = true; server.emit('listening'); } }, delay);
    };
    server.address = () => ({ port: 4242 });
    server.close = callback => { server.closed = true; server.listening = false; callback?.(); };
    return server;
  };
}

test('concurrent evidence server start shares one listener and retries after failure', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-lifecycle-'));
  let attempts = 0;
  const bridge = new KnowledgeBridge({ service: fakeKnowledge(), dataRoot: root,
    applicationId: fakeKnowledge().applicationId, serverFactory: () => {
      attempts += 1; return fakeServer({ fail: attempts === 1 })();
    } });
  try {
    await assert.rejects(bridge.ensureServer(), /listen failed/);
    const [first, second] = await Promise.all([bridge.ensureServer(), bridge.ensureServer()]);
    assert.equal(first, second);
    assert.equal(attempts, 2);
    assert.equal(first.endpoint, 'http://127.0.0.1:4242');
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('close during startup awaits the listener and leaves no server or retry path', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-close-'));
  const created = [];
  const bridge = new KnowledgeBridge({ service: fakeKnowledge(), dataRoot: root,
    applicationId: fakeKnowledge().applicationId, serverFactory: () => {
      const server = fakeServer({ delay: 20 })(); created.push(server); return server;
    } });
  try {
    const starting = bridge.ensureServer();
    await new Promise(resolve => setTimeout(resolve, 1));
    const closing = bridge.close();
    await assert.rejects(starting, /KNOWLEDGE_SERVICE_STOPPING/);
    await closing;
    assert.equal(created.length, 1);
    assert.equal(created[0].closed, true);
    await assert.rejects(bridge.ensureServer(), /KNOWLEDGE_SERVICE_STOPPING/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('citation original requires frozen bytes and extraction versions without leaking newer bytes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-original-'));
  const service = fakeKnowledge();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
  try {
    await bridge.prepareBinding({ commandId: 'original-command', projectId: 'p', mode: 'PROJECT_REFERENCES', projectDocumentIds: ['project-doc'] });
    const document = service.documents.get('project-doc');
    const good = await bridge.original('original-command', 'project-doc');
    assert.equal(good.document.sha256, document.sha256);
    const readOriginal = service.original;
    service.original = async id => {
      const value = await readOriginal(id);
      return { ...value, document: { ...value.document, backendRevision: digest('pdf-v1:wiki-submission') } };
    };
    assert.equal((await bridge.original('original-command', 'project-doc')).document.sha256, document.sha256);
    service.original = readOriginal;
    service.documents.get('unselected-doc').backendRevision = '9'.repeat(64);
    assert.equal((await bridge.original('original-command', 'project-doc')).document.sha256, document.sha256);
    const originalText = document.pages[0].text;
    document.pages[0].text = 'Changed bytes under the old citation.';
    await assert.rejects(bridge.original('original-command', 'project-doc'), /KNOWLEDGE_EVIDENCE_CHANGED/);
    document.pages[0].text = originalText;
    document.backendRevision = '8'.repeat(64);
    await assert.rejects(bridge.original('original-command', 'project-doc'), /KNOWLEDGE_EVIDENCE_CHANGED/);
    document.backendRevision = 'b5f690f8366c785a41cac7db6259935dfa0680a8c0b612748034d3fb0c3dd4ed';
    service.control.onOriginal = current => { current.backendRevision = '7'.repeat(64); };
    await assert.rejects(bridge.original('original-command', 'project-doc'), /KNOWLEDGE_EVIDENCE_CHANGED/);
    service.control.onOriginal = null;
    document.backendRevision = 'b5f690f8366c785a41cac7db6259935dfa0680a8c0b612748034d3fb0c3dd4ed';
    assert.equal((await bridge.original('original-command', 'project-doc')).document.sha256, document.sha256);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('topic citation uses only topicOriginal for the frozen version', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-topic-original-'));
  const service = fakeKnowledge();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
  try {
    await bridge.prepareBinding({ commandId: 'topic-original', projectId: 'p', mode: 'TEAM_KNOWLEDGE',
      team: { kbId: 'kb-team', topicVersionId: 'topic-v1' } });
    const before = service.calls.filter(value => value.op === 'topicOriginal').length;
    const value = await bridge.original('topic-original', '11111111-1111-4111-8111-111111111111', 'topic-v1');
    assert.equal(Buffer.from(value.bytes).toString(), 'topic original');
    assert.equal(service.calls.filter(item => item.op === 'topicOriginal').length, before + 1);
    assert.equal(service.calls.some(item => item.op === 'original'), false);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('analysis accepts unit subsets in caller order and normalizes the submitted identity', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-knowledge-unit-order-'));
  const service = fakeKnowledge();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: service.applicationId });
  try {
    await bridge.prepareBinding({ commandId: 'ordered-command', projectId: 'p', mode: 'PROJECT_REFERENCES', projectDocumentIds: ['project-doc'] });
    const taskRoot = join(root, 'task'); await mkdir(taskRoot, { recursive: true });
    bridge.registerRecord({ commandId: 'ordered-command', python: '/installed/python', capabilityRoot: root,
      prepared: { task_root: taskRoot, workspace: taskRoot, task_id: 'task-ordered' } });
    let submitted;
    bridge.runtime = { dataRoot: root, attestationKey: 'f'.repeat(64), async command(argv) {
      submitted = JSON.parse(await readFile(argv[argv.indexOf('--input') + 1], 'utf8'));
      return JSON.stringify({ status: 'WORKSPACE_OPERATION_SAVED', operation_state: 'COMMITTED' });
    } };
    const ids = ['u2', 'u1'];
    await bridge.analysis('ordered-command', ids.map(unit_id => ({ unit_id, decision: 'PENDING_EVIDENCE',
      reason: 'No product evidence was consumed.', evidence: [], clarification_question: '' })),
    ids, 'workspace-revision-one', 'unordered-batch');
    assert.deepEqual(submitted.unit_ids, ['u1', 'u2']);
    assert.deepEqual(submitted.knowledge_attestation.unit_ids, ['u1', 'u2']);
    assert.deepEqual(ids, ['u2', 'u1']);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});
