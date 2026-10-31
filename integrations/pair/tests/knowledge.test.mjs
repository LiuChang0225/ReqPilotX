import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { KnowledgeBridge } from '../host/src/knowledge.js';
import { TaskLauncher } from '../host/src/task-launch.js';
import { createHandler } from '../host/src/index.js';

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
    bridge.getRecord = () => ({ commandId: 'stable', python: '/installed/python', sourceSha256: 'c'.repeat(64),
      bundleSha: 'd'.repeat(64),
      prepared: { task_root: join(root, 'task'), workspace: join(root, 'task'), task_id: 'task-stable' },
      capabilityRoot: join(root, 'bundle') });
    bridge.runtime = { dataRoot: root, command: async (argv, env, options) => {
      statuses.push(JSON.parse(await readFile(argv[3], 'utf8')));
      return '{"status":"EVIDENCE_STATUS_SAVED"}';
    } };
    await mkdir(join(root, 'task'), { recursive: true });
    await assert.rejects(bridge.search('stable', 'valve', 5), /KNOWLEDGE_EVIDENCE_CHANGED/);
    assert.equal(statuses.length, 1);
    assert.equal(statuses[0].action, 'evidence_status');
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

test('TaskLauncher freezes an adopted library version and exposes the adopted evidence MCP', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-launch-'));
  const source = Buffer.from('customer source'); const sha = digest(source);
  const commandId = '22345678-1234-4234-8234-123456789abc';
  const libraryId = '11111111-1111-4111-8111-111111111111';
  const versionId = 'b'.repeat(64);
  const prepared = []; const adoptedCalls = [];
  const runtime = { dataRoot: root, python: '/installed/python', ownerId: commandId,
    snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:1234' },
    async command(args, env, options) {
      if (args.some(value => String(value).endsWith('task-workflow.py'))) return JSON.stringify({ status: 'READY', task_id: 'task-adopted',
        workflow: { schema: 'reqpilot-fixed-workflow-state/v1', source_sha256: sha,
          workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0', definition_sha256: 'd'.repeat(64),
            brief_sha256: 'f'.repeat(64), evidence_mode: 'ADOPTED_KNOWLEDGE' },
          stages: [{ id: 'PREPARATION', label: '资料准备', status: 'SAVED', evidence: [{ kind: 'TASK_CONTEXT', source_sha256: sha }] }],
          next_stage: 'SOURCE_MAP', ready_to_review: false, verification: 'SAVED_ARTIFACTS_ONLY', open_items: [], human_acceptance: 'NOT_CLAIMED' } });
      const input = JSON.parse(await readFile(args[2], 'utf8')); prepared.push(input);
      await mkdir(input.workspace, { recursive: true });
      const workspace = input.workspace;
      return JSON.stringify({ schema: 'reqpilot-host-launch/v1', workspace, task_id: 'task-adopted', task_root: workspace,
        work_ref: 'plugin-' + 'a'.repeat(32), source_sha256: sha, bundle_sha256: 'b'.repeat(64), prompt: 'Read request.md',
        workflow: { schema: 'reqpilot-fixed-workflow-state/v1', source_sha256: sha,
          workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0', definition_sha256: 'd'.repeat(64),
            brief_sha256: 'f'.repeat(64), evidence_mode: 'ADOPTED_KNOWLEDGE' }, stages: [], ready_to_review: false,
          verification: 'SAVED_ARTIFACTS_ONLY', open_items: [], human_acceptance: 'NOT_CLAIMED' },
        tools: { schema: 'reqpilot-host-tools/v1', mcp: { command: '/installed/python',
          args: [join(workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', workspace, '--source-root', workspace], cwd: workspace } } });
    } };
  const knowledge = {
    available: () => true, adoptionAvailable: () => true,
    prepareAdoptedBinding: async input => {
      adoptedCalls.push(input);
      return { schema_version: 'reqpilot-task-evidence/v1', mode: 'ADOPTED_KNOWLEDGE',
        application_id: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc', project_id: input.projectId,
        created_at: '2026-10-31T00:00:00Z', project: null, team: null,
        adopted: { library_id: input.libraryId, version_id: input.versionId,
          caller: 'reqpilotx-task:reqpilotx-pair:12345678-1234-4234-8234-123456789abc:' + input.commandId + ':adopted',
          name: 'Synthetic adopted library', package_version: '1.0.0' }, binding_sha256: 'c'.repeat(64) };
    },
    bindingPath: id => join(root, 'knowledge-bindings', id + '.binding.json'),
    registerRecord() {}, checkBinding: async () => ({ state: 'CURRENT', message: '' }),
    markStatus: async () => true,
    mcpBinding: async (id, record) => ({ command: record.python, args: ['/knowledge-mcp.py', '/runtime.json'], cwd: record.prepared.workspace, serverName: 'reqpilot_knowledge' }),
  };
  const services = { defaultModel: { currentSelection: () => ({ provider: 'p', model: 'm' }) },
    workspaces: { async create() { return { workspace: { workspaceId: 'workspace-adopted' } }; } },
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
      goal: 'Use the frozen adopted version.', adoptedKnowledge: { libraryId, versionId } });
    assert.equal(receipt.status, 'ACCEPTED');
    assert.equal(receipt.knowledgeEvidence.mode, 'ADOPTED_KNOWLEDGE');
    assert.equal(adoptedCalls.length, 1);
    assert.deepEqual(adoptedCalls[0], { commandId, projectId: 'p', libraryId, versionId });
    assert.equal(prepared[0].evidence_mode, 'ADOPTED_KNOWLEDGE');
    assert.equal(prepared[0].knowledge_evidence.adopted.version_id, versionId);
    const saved = JSON.parse(await readFile(join(root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(saved.workflow.evidence.mode, 'ADOPTED_KNOWLEDGE');
    assert.deepEqual(saved.knowledgeSelection, { adoptedKnowledge: { libraryId, versionId } });
    const bindings = await launcher.toolBindings(receipt.sessionId);
    assert.equal(bindings.length, 2);
    assert.equal(bindings[1].serverName, 'reqpilot_knowledge');
    const listed = await launcher.list({ projectId: 'p', documentId: 'd', sourceSha256: sha });
    assert.deepEqual(listed.tasks[0].request.adoptedKnowledge, { libraryId, versionId });
    await assert.rejects(launcher.start({ commandId, projectId: 'p', documentId: 'd', sourceSha256: sha,
      goal: 'Use the frozen adopted version.', adoptedKnowledge: { libraryId, versionId: 'c'.repeat(64) } }), /COMMAND_CONFLICT/);
  } finally { await launcher.close(); await rm(root, { recursive: true, force: true }); }
});

test('a restarted launcher restores the same adopted choice and retries the same frozen command', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-restart-'));
  const source = Buffer.from('customer source'); const sha = digest(source);
  const commandId = '32345678-1234-4234-8234-123456789abc';
  const libraryId = '11111111-1111-4111-8111-111111111111';
  const versionId = 'b'.repeat(64);
  const created = []; const prompts = []; let failPrompt = true;
  const runtime = { dataRoot: root, python: '/installed/python', ownerId: commandId,
    snapshot: { status: 'READY', mode: 'managed', serviceOrigin: 'http://127.0.0.1:1234' },
    async command(args) {
      if (args.some(value => String(value).endsWith('task-workflow.py'))) return JSON.stringify({ status: 'READY', task_id: 'task-restart',
        workflow: { schema: 'reqpilot-fixed-workflow-state/v1', source_sha256: sha,
          workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0', definition_sha256: 'd'.repeat(64),
            brief_sha256: 'f'.repeat(64), evidence_mode: 'ADOPTED_KNOWLEDGE' },
          stages: [], next_stage: 'SOURCE_MAP', ready_to_review: false, verification: 'SAVED_ARTIFACTS_ONLY',
          open_items: [], human_acceptance: 'NOT_CLAIMED' } });
      const input = JSON.parse(await readFile(args[2], 'utf8'));
      await mkdir(input.workspace, { recursive: true });
      return JSON.stringify({ schema: 'reqpilot-host-launch/v1', workspace: input.workspace, task_id: 'task-restart',
        task_root: input.workspace, work_ref: 'plugin-' + 'a'.repeat(32), source_sha256: sha, bundle_sha256: 'b'.repeat(64),
        prompt: 'Read request.md', workflow: { schema: 'reqpilot-fixed-workflow-state/v1', source_sha256: sha,
          workflow: { id: 'reqpilot-fixed-workflow', version: '1.0.0', definition_sha256: 'd'.repeat(64),
            brief_sha256: 'f'.repeat(64), evidence_mode: 'ADOPTED_KNOWLEDGE' }, stages: [], ready_to_review: false,
          verification: 'SAVED_ARTIFACTS_ONLY', open_items: [], human_acceptance: 'NOT_CLAIMED' },
        tools: { schema: 'reqpilot-host-tools/v1', mcp: { command: '/installed/python',
          args: [join(input.workspace, '.reqpilot/scripts/mcp_server.py'), '--working-root', input.workspace, '--source-root', input.workspace], cwd: input.workspace } } });
    } };
  const knowledge = {
    available: () => true, adoptionAvailable: () => true,
    prepareAdoptedBinding: async input => ({ schema_version: 'reqpilot-task-evidence/v1', mode: 'ADOPTED_KNOWLEDGE',
      application_id: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc', project_id: input.projectId,
      created_at: '2026-10-31T00:00:00Z', project: null, team: null,
      adopted: { library_id: input.libraryId, version_id: input.versionId,
        caller: 'reqpilotx-task:reqpilotx-pair:12345678-1234-4234-8234-123456789abc:' + input.commandId + ':adopted',
        name: 'Synthetic adopted library', package_version: '1.0.0' }, binding_sha256: 'c'.repeat(64) }),
    bindingPath: id => join(root, 'knowledge-bindings', id + '.binding.json'),
    registerRecord() {}, checkBinding: async () => ({ state: 'CURRENT', message: '' }), markStatus: async () => true,
    mcpBinding: async (id, record) => ({ command: record.python, args: ['/knowledge-mcp.py', '/runtime.json'],
      cwd: record.prepared.workspace, serverName: 'reqpilot_knowledge' }),
  };
  const services = { defaultModel: { currentSelection: () => ({ provider: 'p', model: 'm' }) },
    workspaces: { async create() { return { workspace: { workspaceId: 'workspace-restart' } }; } },
    sessions: {
      async create(value) { created.push(value.sessionId); return { sessionId: value.sessionId }; },
      async inspect() { throw Object.assign(Error('session/not-found'), { code: 'session/not-found' }); },
      async selectSessionModel({ sessionId, ...selected }) { return { selected }; },
      async prompt(input) { prompts.push(input); if (failPrompt) { failPrompt = false; throw Error('acknowledgement lost'); } return { accepted: true }; },
    } };
  const transport = async url => url.endsWith('/file') ? new Response(source)
    : new Response(JSON.stringify([{ id: 'd', file_name: 'source.pdf', source_sha256: sha }]));
  const configure = launcher => { launcher.ready = true; launcher.capabilityRoot = root; launcher.bundleSha = 'b'.repeat(64);
    launcher.workflow = { schema: 'reqpilot-fixed-workflow/v1', id: 'reqpilot-fixed-workflow', version: '1.0.0',
      definition_sha256: 'd'.repeat(64), evidence: { mode: 'NONE', reason: 'INTERNAL_EVIDENCE_NOT_WIRED' } }; return launcher; };
  const first = configure(new TaskLauncher(runtime, { taskMcp: true }, () => services, transport, knowledge));
  const request = { commandId, projectId: 'p', documentId: 'd', sourceSha256: sha,
    goal: 'Use the frozen adopted version.', adoptedKnowledge: { libraryId, versionId } };
  try {
    await assert.rejects(first.start(request), /acknowledgement lost/);
    const saved = JSON.parse(await readFile(join(root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(saved.phase, 'START_REQUESTED');
    assert.deepEqual(saved.knowledgeSelection, { adoptedKnowledge: { libraryId, versionId } });
    const second = configure(new TaskLauncher(runtime, { taskMcp: true }, () => services, transport, knowledge));
    const listed = await second.list({ projectId: 'p', documentId: 'd', sourceSha256: sha });
    assert.deepEqual(listed.tasks[0].request.adoptedKnowledge, { libraryId, versionId });
    assert.equal(listed.tasks[0].request.knowledge, undefined);
    const receipt = await second.start(listed.tasks[0].request);
    assert.equal(receipt.status, 'ACCEPTED');
    assert.ok(created.every(id => id === saved.sessionId));
    assert.ok(prompts.every(item => item.requestId === saved.requestId));
    assert.equal(prompts.length, 2);
    assert.equal(prompts[0].sessionId, prompts[1].sessionId);
    const final = JSON.parse(await readFile(join(root, 'task-launches', commandId + '.json'), 'utf8'));
    assert.equal(final.phase, 'ACCEPTED');
    assert.deepEqual(final.knowledgeBinding.adopted.version_id, versionId);
    assert.equal(final.selectedAgentModel.model, 'm');
  } finally { await first.close(); await first.close(); await rm(root, { recursive: true, force: true }); }
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
      sourceSha256: 'd'.repeat(64), bundleSha: 'e'.repeat(64),
      prepared: { task_root: taskRoot, workspace: taskRoot, task_id: 'task-analysis' } });
    const calls = [];
    bridge.runtime = { dataRoot: root, attestationKey: 'f'.repeat(64), async command(argv, env) {
      const payload = JSON.parse(await readFile(argv[3], 'utf8'));
      calls.push({ argv, env, payload });
      assert.equal(env.REQPILOT_HOST_ATTESTATION_KEY, 'f'.repeat(64));
      const request = JSON.parse(await readFile(payload.request_file, 'utf8'));
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
    assert.equal(calls[0].payload.action, 'analysis');
    const firstRequest = JSON.parse(await readFile(calls[0].payload.request_file, 'utf8'));
    assert.deepEqual(firstRequest.knowledge_attestation.decisions.map(item => item.unit_id), ['u1']);
    const second = await bridge.analysis('analysis-command', [{ unit_id: 'u2', decision: 'PENDING_EVIDENCE',
      reason: 'No suitable evidence for this unit.', evidence: [],
      clarification_question: 'Provide current evidence.' }], ['u2'], 'workspace-revision-two', 'model-command-two');
    assert.equal(second.analysis.assessed_unit_count, 1);
    assert.equal(calls.length, 2);
    const secondRequest = JSON.parse(await readFile(calls[1].payload.request_file, 'utf8'));
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

test('adopted evidence routes are task-caller scoped and return versioned facts', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-http-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  try {
    await bridge.prepareAdoptedBinding({ commandId: 'adopted-http', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    bridge.registerRecord({ commandId: 'adopted-http', prepared: { workspace: root, task_root: root, task_id: 'task-adopted' } });
    const runtime = JSON.parse(await readFile(await bridge.runtimeFile('adopted-http'), 'utf8'));
    const post = (route, body) => fetch(runtime.endpoint + route, { method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer ' + runtime.token },
      body: JSON.stringify(body) }).then(response => response.json());
    const status = await post('/evidence/adopted/status', {});
    assert.equal(status.ok, true);
    assert.equal(status.result.versionId, 'b'.repeat(64));
    const browse = await post('/evidence/adopted/browse', { section: 'claims', limit: 5 });
    assert.equal(browse.result.items[0].id, 'claim-1');
    const evidence = await post('/evidence/adopted/evidence', { query: 'allowable', limit: 2 });
    assert.equal(evidence.result.facts.length, 2);
    assert.ok(evidence.result.facts.every(fact => fact.adopted.version_id === 'b'.repeat(64)));
    const source = await post('/evidence/adopted/read', { kind: 'source', sourceId: 'source-1' });
    assert.equal(source.result.fact.evidence_origin, 'derived');
    assert.equal(source.result.fact.authority_verified, false);
    assert.equal(source.result.fact.content_revision, service.normalizedRevision);
    assert.equal(source.result.fact.source_version, service.originalRevision);
    const legacy = await post('/evidence/search', { query: 'allowable' });
    assert.equal(legacy.error.code, 'ADOPTED_TOOLS_REQUIRED');
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('every adopted fact kind resolves through its exact versioned record route', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-records-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  const server = createServer(createHandler(null, () => undefined, fetch, () => ({ mode: 'managed', status: 'READY' }), null, bridge));
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const origin = 'http://127.0.0.1:' + server.address().port;
  try {
    await bridge.prepareAdoptedBinding({ commandId: 'adopted-records', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    const evidence = await bridge.adoptedEvidence('adopted-records', 'allowable load', 8);
    const sourceRead = await bridge.adoptedRead('adopted-records', { kind: 'source', sourceId: 'source-1' });
    const facts = [...evidence.facts, sourceRead.fact];
    const expected = { claim: 'claim-1', experience: 'exp-1', conflict: 'conflict-1', gap: 'gap-1', wiki: 'guide.md', source: 'source-1' };
    for (const fact of facts) {
      const kind = fact.adopted.kind;
      assert.match(fact.url, /\/adopted\/records\?/);
      const response = await fetch(origin + fact.url);
      assert.equal(response.status, 200, kind + ': ' + fact.url);
      const body = await response.json();
      assert.equal(body.kind, kind);
      assert.equal(body.id, expected[kind]);
    }
    const claim = facts.find(fact => fact.adopted.kind === 'claim');
    const linked = await fetch(origin + claim.linked_source_url);
    assert.equal(linked.status, 200);
    assert.equal(linked.headers.get('content-type'), 'application/pdf');
    assert.equal(linked.headers.get('x-reqpilot-adopted-content-revision'), service.originalRevision);
    assert.equal(digest(Buffer.from(await linked.arrayBuffer())), service.originalRevision);
    const primary = await fetch(origin + claim.url);
    const primaryBody = await primary.json();
    assert.equal(primaryBody.kind, 'claim');
    assert.equal(primaryBody.sourceId, 'source-1');
    assert.notEqual(digest(Buffer.from(JSON.stringify(primaryBody))), service.originalRevision);
  } finally {
    await new Promise(resolve => server.close(resolve));
    await bridge.close(); await rm(root, { recursive: true, force: true });
  }
});

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
      sourceSha256: 'd'.repeat(64), bundleSha: 'e'.repeat(64),
      prepared: { task_root: taskRoot, workspace: taskRoot, task_id: 'task-ordered' } });
    let submitted;
    bridge.runtime = { dataRoot: root, attestationKey: 'f'.repeat(64), async command(argv) {
      const payload = JSON.parse(await readFile(argv[3], 'utf8'));
      submitted = JSON.parse(await readFile(payload.request_file, 'utf8'));
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


function fakeAdopted() {
  const library = { id: '11111111-1111-4111-8111-111111111111', name: 'Synthetic adopted library',
    purpose: 'Synthetic consumer fixture', operation: 'connect-existing', packageVersion: '1.0.0',
    okfVersion: '1.0', versionId: 'b'.repeat(64), state: 'ready', recommendation: 'direct-use',
    addedAt: '2026-10-31T00:00:00.000Z', lastCheckedAt: '2026-10-31T00:00:00.000Z',
    counts: { sources: 1, claims: 1, entities: 0, relations: 0, images: 0, experiences: 1, conflicts: 1, gaps: 1, wikiPages: 1, files: 4 },
    checks: [{ id: 'manifest', state: 'ok', summary: 'Synthetic manifest is complete.' }] };
  const normalizedText = 'SYNTHETIC ADOPTED NORMALIZED CONTENT: the allowable load is 10 kN at the declared condition.';
  const normalizedRevision = digest(normalizedText);
  const originalBytes = Buffer.from([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x00, 0x01, 0x02, 0x03]);
  const originalRevision = digest(originalBytes);
  const declaredOriginalRevision = 'd'.repeat(64);
  const claimsRevision = digest('synthetic claims table');
  const imagesRevision = digest('synthetic images table');
  const derivedBytes = Buffer.from('%PDF-1.7\napproved derivative bytes');
  const derivedRevision = digest(derivedBytes);
  const imageBytes = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0x01]);
  const experiencesRevision = digest('synthetic experiences table');
  const conflictsRevision = digest('synthetic conflicts table');
  const gapsRevision = digest('synthetic gaps table');
  const wikiRevision = digest('synthetic wiki page');
  const claim = { id: 'claim-1', subject: 'bearing', predicate: 'allowable_load', value: '10 kN',
    source_id: 'source-1', scope: 'Synthetic scope', conditions: 'Synthetic condition',
    review: { method: 'manual', reviewStatus: 'pending', expertApproved: 'false' } };
  const records = {
    claim: { kind: 'claim', id: 'claim-1', title: 'bearing', record: claim, revision: claimsRevision },
    experience: { kind: 'experience', id: 'exp-1', title: 'Synthetic experience',
      record: { id: 'exp-1', summary: 'Attributed experience, not proof.' }, revision: experiencesRevision },
    conflict: { kind: 'conflict', id: 'conflict-1', title: 'Synthetic conflict',
      record: { id: 'conflict-1', status: 'open' }, revision: conflictsRevision },
    gap: { kind: 'gap', id: 'gap-1', title: 'Synthetic gap',
      record: { id: 'gap-1', severity: 'medium' }, revision: gapsRevision },
    image: { kind: 'image', id: 'image-1', title: 'Synthetic figure', path: 'images/figure-1.png',
      record: { id: 'image-1', image_path: 'images/figure-1.png' }, revision: imagesRevision },
    wiki: { kind: 'wiki', id: 'guide.md', path: 'guide.md', title: 'Synthetic guide',
      content: '# Synthetic guide\n\nDerived wiki content.', revision: wikiRevision },
  };
  const bindings = new Map(); const calls = [];
  const control = { metadataOnly: false, noNormalized: false, derived: false, metadataError: null };
  return {
    calls, control, normalizedText, normalizedRevision, originalBytes, originalRevision, declaredOriginalRevision,
    claimsRevision, experiencesRevision, conflictsRevision, gapsRevision, imagesRevision, wikiRevision,
    derivedBytes, derivedRevision, imageBytes, sourceText: normalizedText,
    adoptedLibraries: () => [{ ...library }],
    bindAdoptedSession: async (caller, libraryId) => {
      if (libraryId !== library.id) throw Object.assign(Error('not-found'), { code: 'not-found' });
      const value = { sessionId: caller, libraryId, versionId: library.versionId,
        operation: 'connect-existing', boundAt: '2026-10-31T00:00:00.000Z' };
      bindings.set(caller, value); calls.push({ op: 'bind', caller, libraryId });
      return value;
    },
    adoptedBinding: caller => bindings.get(caller),
    browseAdopted: async (caller, section) => {
      if (!bindings.has(caller)) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
      return { libraryId: library.id, versionId: library.versionId, section, total: 1, truncated: false,
        items: [{ id: section === 'wiki' ? 'guide.md' : 'claim-1', title: section === 'wiki' ? 'guide' : 'bearing' }] };
    },
    readAdopted: async (caller, request) => {
      const binding = bindings.get(caller);
      if (!binding) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
      if (request.kind === 'source') return { libraryId: library.id, versionId: library.versionId,
        packageVersion: '1.0.0', kind: 'source', id: request.sourceId, sourceId: request.sourceId,
        title: 'Original note', content: control.metadataOnly || control.noNormalized ? '' : normalizedText,
        locator: { kind: 'logical-section', section: '2.1' },
        availability: control.metadataOnly ? 'metadata-only' : control.derived ? 'derived' : 'original',
        admission: 'full_original',
        sourceVersion: control.derived ? derivedRevision : originalRevision,
        originalSourceVersion: declaredOriginalRevision,
        contentRevision: control.derived ? derivedRevision : control.noNormalized ? originalRevision : normalizedRevision };
      if (request.kind === 'wiki') return { libraryId: library.id, versionId: library.versionId,
        packageVersion: '1.0.0', kind: 'wiki', id: request.path, path: request.path, title: 'Synthetic guide',
        content: records.wiki.content, contentRevision: wikiRevision,
        locator: { kind: 'logical-section', section: request.path }, availability: 'derived' };
      if (request.kind === 'image' && control.metadataError) {
        if (control.metadataError === 'version-changed') library.versionId = 'c'.repeat(64);
        if (control.metadataError === 'library-unselected') bindings.delete(caller);
        throw Object.assign(Error(control.metadataError), { code: control.metadataError });
      }
      const record = records[request.kind];
      if (!record) throw Object.assign(Error('source-unavailable'), { code: 'source-unavailable' });
      return { libraryId: library.id, versionId: library.versionId, packageVersion: '1.0.0',
        kind: record.kind, id: record.id, title: record.title, record: record.record,
        ...(record.kind === 'claim' ? { sourceId: 'source-1', sourceVersion: originalRevision,
          originalSourceVersion: declaredOriginalRevision, admission: 'full_original',
          review: claim.review, scope: claim.scope, conditions: claim.conditions,
          availability: control.metadataOnly ? 'metadata-only' : 'original' } : {}),
        locator: record.kind === 'image'
          ? { kind: 'image-region', imagePath: record.path, bbox: [10, 20, 30, 40] }
          : { kind: 'logical-section', section: '2.1' },
        ...(record.kind === 'image' ? { availability: 'original' } : {}),
        contentRevision: record.kind === 'claim' && control.metadataOnly ? claimsRevision : record.revision };
    },
    adoptedEvidence: async (caller, query, limit) => {
      if (!bindings.has(caller)) throw Object.assign(Error('library-unselected'), { code: 'library-unselected' });
      return { version: 1, libraryId: library.id, versionId: library.versionId, packageVersion: '1.0.0',
        query, coverage: { scanned: 5, truncated: false }, warnings: [], insufficiencies: [],
        items: [
          { evidenceId: 'claim:claim-1', kind: 'claim', title: 'bearing',
            excerpt: 'bearing allowable_load 10 kN at Synthetic condition', sourceId: 'source-1',
            sourceVersion: originalRevision, packageVersion: '1.0.0',
            locator: { kind: 'logical-section', section: '2.1' }, review: claim.review,
            origin: 'derived', scope: claim.scope, conditions: claim.conditions,
            availability: 'original', admission: 'full_original', originalSourceVersion: declaredOriginalRevision,
            contentRevision: claimsRevision },
          { evidenceId: 'experience:exp-1', kind: 'experience', title: 'Synthetic experience',
            excerpt: 'Field experience is attributed and is not proof of an original.',
            packageVersion: '1.0.0', origin: 'experience', contentRevision: experiencesRevision },
          { evidenceId: 'wiki:guide.md', kind: 'wiki', title: 'Synthetic guide',
            excerpt: 'Derived wiki guidance.', packageVersion: '1.0.0', origin: 'derived', contentRevision: wikiRevision },
          { evidenceId: 'conflicts:conflict-1', kind: 'conflict', title: 'Synthetic conflict',
            excerpt: 'Unresolved conflict stays visible.', packageVersion: '1.0.0', origin: 'conflict', contentRevision: conflictsRevision },
          { evidenceId: 'gaps:gap-1', kind: 'gap', title: 'Synthetic gap',
            excerpt: 'Open evidence gap stays explicit.', packageVersion: '1.0.0', origin: 'gap', contentRevision: gapsRevision },
        ].slice(0, limit) };
    },
    adoptedSource: async (libraryId, versionId, sourceId, caller) => {
      const binding = bindings.get(caller);
      if (libraryId !== library.id || versionId !== library.versionId || binding?.versionId !== versionId)
        throw Object.assign(Error('version-changed'), { code: 'version-changed' });
      if (control.metadataOnly) throw Object.assign(Error('metadata-only'), { code: 'metadata-only' });
      if (sourceId !== 'source-1') throw Object.assign(Error('source-unavailable'), { code: 'source-unavailable' });
      if (control.derived) return { bytes: derivedBytes, mime: 'application/pdf', filename: 'approved-derivative.pdf',
        availability: 'derived', contentRevision: derivedRevision };
      return { bytes: originalBytes, mime: 'application/pdf', filename: 'note.pdf', availability: 'original',
        contentRevision: originalRevision };
    },
    adoptedImage: async () => ({ bytes: imageBytes, mime: 'image/png' }),
    setVersion: versionId => { library.versionId = versionId; },
  };
}

test('adopted selection freezes the exact library version on a task-owned caller', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-binding-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  try {
    const binding = await bridge.prepareAdoptedBinding({ commandId: 'adopted-command', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    assert.equal(binding.mode, 'ADOPTED_KNOWLEDGE');
    assert.equal(binding.adopted.version_id, 'b'.repeat(64));
    assert.equal(binding.adopted.caller, 'reqpilotx-task:reqpilotx-pair:12345678-1234-4234-8234-123456789abc:adopted-command:adopted');
    assert.notEqual(binding.adopted.caller, 'session-native');
    assert.deepEqual(service.calls.filter(value => value.op === 'bind').map(value => value.caller), [binding.adopted.caller]);
    assert.equal((await bridge.checkBinding('adopted-command')).state, 'CURRENT');
    await assert.rejects(bridge.prepareAdoptedBinding({ commandId: 'adopted-command', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'c'.repeat(64) }), /KNOWLEDGE_BINDING_CONFLICT/);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('adopted evidence preserves derived origin, versions, review and honest insufficiency', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-evidence-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  try {
    await bridge.prepareAdoptedBinding({ commandId: 'adopted-evidence', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    const result = await bridge.adoptedEvidence('adopted-evidence', 'allowable load', 8);
    assert.equal(result.facts.length, 5);
    const claim = result.facts.find(fact => fact.adopted.kind === 'claim');
    const experience = result.facts.find(fact => fact.adopted.kind === 'experience');
    const wiki = result.facts.find(fact => fact.adopted.kind === 'wiki');
    const conflict = result.facts.find(fact => fact.adopted.kind === 'conflict');
    const gap = result.facts.find(fact => fact.adopted.kind === 'gap');
    assert.equal(claim.evidence_origin, 'derived');
    assert.equal(claim.authority_verified, false);
    assert.equal(claim.authority, 'UNKNOWN');
    assert.equal(claim.source_sha256, service.claimsRevision);
    assert.equal(claim.content_revision, service.claimsRevision);
    assert.equal(claim.source_version, service.originalRevision);
    assert.equal(claim.original_source_version, service.declaredOriginalRevision);
    assert.equal(claim.original_revision, undefined);
    assert.equal(claim.review.expertApproved, 'false');
    assert.equal(claim.expert_approved_declared, false);
    assert.equal(claim.source_ref.location.page_number, null);
    assert.equal(claim.source_ref.location.section, '2.1');
    assert.equal(claim.document_id, 'claim:claim-1');
    assert.match(claim.url, /\/adopted\/records\?kind=claim&id=claim-1$/);
    assert.match(claim.linked_source_url, /\/adopted\/source\/source-1$/);
    assert.equal(experience.evidence_origin, 'experience');
    assert.equal(experience.availability, 'not-applicable');
    assert.equal(wiki.evidence_origin, 'derived');
    assert.equal(conflict.evidence_origin, 'conflict');
    assert.equal(gap.evidence_origin, 'gap');
    for (const fact of [experience, wiki, conflict, gap]) {
      assert.match(fact.url, /\/adopted\/records\?/);
      assert.equal(fact.linked_source_url, undefined);
    }
    const catalog = await bridge.catalog('adopted-evidence');
    assert.equal(catalog.registration.mode, 'ADOPTED_KNOWLEDGE');
    assert.equal(catalog.catalog.documents[0].adopted_library_version_id, 'b'.repeat(64));
    assert.equal(Object.keys(catalog.catalog.evidence_catalog).length, 5);

    service.control.metadataOnly = true;
    const metadata = await bridge.adoptedEvidence('adopted-evidence', 'allowable load', 8);
    assert.equal(metadata.facts.length, 5);
    const source = await bridge.adoptedRead('adopted-evidence', { kind: 'source', sourceId: 'source-1' }).catch(error => error);
    assert.match(String(source.message), /ADOPTED_METADATA_ONLY/);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('normalized adopted content and original bytes keep independent revisions and no authority promotion', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-read-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  try {
    await bridge.prepareAdoptedBinding({ commandId: 'adopted-read', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    const read = await bridge.adoptedRead('adopted-read', { kind: 'source', sourceId: 'source-1' });
    assert.equal(read.result.content, service.normalizedText);
    assert.equal(read.fact.authority_verified, false);
    assert.equal(read.fact.authority, 'UNKNOWN');
    assert.equal(read.fact.evidence_origin, 'derived');
    assert.equal(read.fact.original_read_proof, 'NOT_READ');
    assert.equal(read.fact.original_revision, undefined);
    assert.equal(read.fact.content_revision, service.normalizedRevision);
    assert.equal(read.fact.source_version, service.originalRevision);
    assert.equal(read.fact.original_source_version, service.declaredOriginalRevision);
    assert.equal(read.fact.returned_content_sha256, digest(service.normalizedText));
    assert.match(read.fact.url, /\/adopted\/records\?kind=source&id=source-1$/);
    assert.match(read.fact.linked_source_url, /\/adopted\/source\/source-1$/);
    assert.equal(read.returnedContentSha256, digest(service.normalizedText));
    assert.notEqual(service.normalizedRevision, service.originalRevision);
    const wiki = await bridge.adoptedRead('adopted-read', { kind: 'wiki', path: 'guide.md' });
    assert.equal(wiki.fact.evidence_origin, 'derived');
    assert.equal(wiki.fact.authority_verified, false);
    assert.equal(wiki.fact.content_revision, service.wikiRevision);
    const source = await bridge.adoptedSource('adopted-read', 'source-1');
    assert.equal(digest(source.bytes), service.originalRevision);
    assert.equal(source.contentRevision, service.originalRevision);
    assert.equal(source.mime, 'application/pdf');
    assert.notEqual(source.contentRevision, read.fact.content_revision);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('adopted version changes and cumulative attestation cannot silently retarget', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-version-'));
  const service = fakeAdopted();
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc' });
  try {
    await bridge.prepareAdoptedBinding({ commandId: 'adopted-version', projectId: 'p',
      libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
    const taskRoot = join(root, 'task'); await mkdir(taskRoot, { recursive: true });
    const requests = [];
    bridge.registerRecord({ commandId: 'adopted-version', documentId: 'doc', sourceSha256: 'd'.repeat(64),
      bundleSha: 'e'.repeat(64), prepared: { task_root: taskRoot,
      workspace: taskRoot, task_id: 'task-adopted', work_ref: 'plugin-' + 'a'.repeat(32) },
    workspace: taskRoot, capabilityRoot: join(root, 'bundle'), python: '/installed/python' });
    bridge.runtime = { dataRoot: root, attestationKey: 'a'.repeat(64),
      command: async (argv, env, options) => {
        const payload = JSON.parse(await readFile(argv[3], 'utf8'));
        const input = JSON.parse(await readFile(payload.request_file, 'utf8')); requests.push(input);
        return JSON.stringify({ status: 'SAVED', analysis: { assessed_unit_count: 2 } });
      } };
    const first = await bridge.adoptedEvidence('adopted-version', 'allowable load', 8);
    const source = await bridge.adoptedRead('adopted-version', { kind: 'source', sourceId: 'source-1' });
    const claim = first.facts.find(fact => fact.adopted.kind === 'claim');
    assert.ok(claim && source.fact);
    const decisions = [
      { unit_id: 'u1', decision: 'PENDING_EVIDENCE', reason: 'The derived claim is cited but does not prove the linked original.',
        evidence: [{ fact_id: claim.fact_id, quote: claim.text }], clarification_question: 'Provide the applicable controlled source.' },
      { unit_id: 'u2', decision: 'NEEDS_CLARIFICATION', reason: 'The normalized content is a derived record, not project authority.',
        evidence: [{ fact_id: source.fact.fact_id, quote: source.fact.text }], clarification_question: 'Confirm the applicable controlled source.' },
    ];
    const saved = await bridge.analysis('adopted-version', decisions, ['u1', 'u2'], 'workspace-revision-one', 'analysis-one');
    assert.equal(saved.status, 'SAVED');
    const attestation = requests[0].knowledge_attestation;
    assert.equal(attestation.mode, 'ADOPTED_KNOWLEDGE');
    assert.deepEqual([...attestation.consumed_fact_ids].sort(), [...first.facts.map(fact => fact.fact_id),
      source.fact.fact_id].sort());
    assert.equal(attestation.decisions.length, 2);
    service.setVersion('c'.repeat(64));
    assert.equal((await bridge.checkBinding('adopted-version')).state, 'CHANGED');
    const stale = await bridge.adoptedEvidence('adopted-version', 'allowable load', 8).catch(error => error);
    assert.match(String(stale.message), /ADOPTED_VERSION_CHANGED/);
    const history = JSON.parse(await readFile(join(root, 'knowledge-analysis', 'adopted-version.history.json'), 'utf8'));
    assert.equal(history.entries.length, 1);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

const mediaApplicationId = 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc';
async function mediaBridge(root, service) {
  const bridge = new KnowledgeBridge({ service, dataRoot: root, applicationId: mediaApplicationId });
  await bridge.prepareAdoptedBinding({ commandId: 'media-task', projectId: 'p',
    libraryId: service.adoptedLibraries()[0].id, versionId: 'b'.repeat(64) });
  return bridge;
}

test('adopted source media returns exact binary bytes when normalized content is unavailable', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-source-'));
  const service = fakeAdopted(); service.control.noNormalized = true;
  const bridge = await mediaBridge(root, service);
  try {
    const read = await bridge.adoptedRead('media-task', { kind: 'source', sourceId: 'source-1' }).catch(error => error);
    assert.match(String(read.message), /ADOPTED_SOURCE_CONTENT_UNAVAILABLE/);
    assert.match(String(read.message), /knowledge_adopted_source/);
    const media = await bridge.adoptedSourceMedia('media-task', 'source-1');
    assert.equal(media.availability, 'original');
    assert.equal(media.contentRevision, service.originalRevision);
    assert.equal(media.contentRevisionBasis, 'BINARY_FILE');
    assert.equal(media.bytesSha256, service.originalRevision);
    assert.equal(media.mime, 'application/pdf');
    assert.equal(media.filename, 'note.pdf');
    assert.equal(media.locator.kind, 'logical-section');
    assert.equal(media.locator.location.page_number, null);
    assert.equal(media.authority, 'MEDIA_BYTES_NOT_EVIDENCE');
    assert.equal(media.humanAcceptance, 'NOT_CLAIMED');
    assert.deepEqual(await readFile(media.path), service.originalBytes);
    assert.equal((await stat(media.path)).mode & 0o222, 0);
    assert.ok(media.path.startsWith(join(root, 'task-media', 'media-task')));
    const consumed = await bridge.loadConsumption('media-task');
    assert.deepEqual(consumed.facts, {});
    assert.deepEqual(consumed.receipts, []);
    const receipts = JSON.parse(await readFile(bridge.mediaReceiptPath('media-task'), 'utf8'));
    assert.equal(receipts.entries.length, 1);
    assert.equal(receipts.entries[0].tool, 'adopted_source');
    assert.equal(receipts.entries[0].bytes_sha256, service.originalRevision);
    assert.equal(receipts.entries[0].authority, 'MEDIA_BYTES_NOT_EVIDENCE');
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('approved-derived bytes stay distinct from normalized records and metadata-only sources are refused', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-derived-'));
  const service = fakeAdopted(); service.control.derived = true;
  const bridge = await mediaBridge(root, service);
  try {
    const media = await bridge.adoptedSourceMedia('media-task', 'source-1');
    assert.equal(media.availability, 'derived');
    assert.equal(media.contentRevision, service.derivedRevision);
    assert.equal(media.bytesSha256, service.derivedRevision);
    assert.notEqual(media.contentRevision, service.normalizedRevision);
    assert.equal(media.filename, 'approved-derivative.pdf');
    assert.deepEqual(await readFile(media.path), service.derivedBytes);
    service.control.metadataOnly = true;
    await assert.rejects(bridge.adoptedSourceMedia('media-task', 'source-1'), /ADOPTED_METADATA_ONLY/);
    const receipts = JSON.parse(await readFile(bridge.mediaReceiptPath('media-task'), 'utf8'));
    assert.equal(receipts.entries.length, 1);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('adopted media refuses changed versions and unbound tasks without caching or facts', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-version-'));
  const service = fakeAdopted();
  const bridge = await mediaBridge(root, service);
  try {
    await assert.rejects(bridge.adoptedSourceMedia('unbound-task', 'source-1'), /KNOWLEDGE_BINDING_MISSING/);
    service.setVersion('c'.repeat(64));
    await assert.rejects(bridge.adoptedSourceMedia('media-task', 'source-1'), /ADOPTED_VERSION_CHANGED/);
    const consumed = await bridge.loadConsumption('media-task');
    assert.deepEqual(consumed.facts, {});
    assert.deepEqual(consumed.receipts, []);
    await assert.rejects(readFile(bridge.mediaReceiptPath('media-task')), error => error.code === 'ENOENT');
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('adopted image media keeps image identity and locator without inventing a page number', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-image-'));
  const service = fakeAdopted();
  const bridge = await mediaBridge(root, service);
  try {
    const media = await bridge.adoptedImageMedia('media-task', 'image-1');
    assert.equal(media.kind, 'image');
    assert.equal(media.id, 'image-1');
    assert.equal(media.libraryId, service.adoptedLibraries()[0].id);
    assert.equal(media.versionId, 'b'.repeat(64));
    assert.equal(media.mime, 'image/png');
    assert.equal(media.filename, 'figure-1.png');
    assert.equal(media.bytesSha256, digest(service.imageBytes));
    assert.equal(media.metadata, 'AVAILABLE');
    assert.equal(media.recordContentRevision, service.imagesRevision);
    assert.equal(media.recordContentRevisionBasis, 'RECORD_TABLE_NOT_BINARY_BYTES');
    assert.equal(media.contentRevision, undefined);
    assert.notEqual(media.recordContentRevision, media.bytesSha256);
    assert.equal(media.locator.kind, 'image-region');
    assert.equal(media.locator.location.page_number, null);
    assert.equal(media.locator.location.section, null);
    assert.equal(media.locator.location.number, null);
    assert.deepEqual(await readFile(media.path), service.imageBytes);
    const receipts = JSON.parse(await readFile(bridge.mediaReceiptPath('media-task'), 'utf8'));
    assert.equal(receipts.entries[0].tool, 'adopted_image');
    assert.equal(receipts.entries[0].record_content_revision, service.imagesRevision);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

test('adopted image metadata owner failures are not swallowed and leave no accepted receipt', async () => {
  for (const [code, expected] of [['version-changed', /ADOPTED_VERSION_CHANGED/],
    ['library-unselected', /ADOPTED_LIBRARY_UNSELECTED/]]) {
    const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-metadata-'));
    const service = fakeAdopted();
    const bridge = await mediaBridge(root, service);
    try {
      service.control.metadataError = code;
      await assert.rejects(bridge.adoptedImageMedia('media-task', 'image-1'), expected, code);
      await assert.rejects(readFile(bridge.mediaReceiptPath('media-task')), error => error.code === 'ENOENT', code);
      const consumed = await bridge.loadConsumption('media-task');
      assert.deepEqual(consumed.facts, {}, code);
      assert.deepEqual(consumed.receipts, [], code);
    } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
  }
});

test('adopted image metadata absence is explicit rather than a swallowed owner error', async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-metadata-absent-'));
  const service = fakeAdopted();
  const bridge = await mediaBridge(root, service);
  try {
    service.control.metadataError = 'source-unavailable';
    const media = await bridge.adoptedImageMedia('media-task', 'image-1');
    assert.equal(media.metadata, 'UNAVAILABLE');
    assert.equal(media.availability, null);
    assert.equal(media.recordContentRevision, null);
    assert.equal(media.locator.kind, 'document');
    assert.equal(media.locator.location.page_number, null);
    assert.equal(media.bytesSha256, digest(service.imageBytes));
    assert.deepEqual(await readFile(media.path), service.imageBytes);
    const receipts = JSON.parse(await readFile(bridge.mediaReceiptPath('media-task'), 'utf8'));
    assert.equal(receipts.entries.length, 1);
    assert.equal(receipts.entries[0].record_content_revision, null);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});

const mcpPython = process.env.PAIR_HOST_COMMAND_PYTHON;
test('actual adopted media MCP dispatch reaches bounded Host routes and logs the media result', { skip: !mcpPython }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'reqpilot-adopted-media-mcp-'));
  const service = fakeAdopted();
  const bridge = await mediaBridge(root, service);
  const runtimeFile = await bridge.runtimeFile('media-task');
  const mcpScript = fileURLToPath(new URL('../host/src/knowledge-mcp.py', import.meta.url));
  const driver = join(root, 'mcp-dispatch-driver.py');
  await writeFile(driver, `import asyncio, importlib.util, json, sys, types
mcp = types.ModuleType('mcp'); server_mod = types.ModuleType('mcp.server'); types_mod = types.ModuleType('mcp.types')
class MCPServer:
    def __init__(self, *args, **kwargs): pass
class Tool:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class ToolAnnotations:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class CallToolResult:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class TextContent:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
server_mod.MCPServer = MCPServer
types_mod.Tool = Tool; types_mod.ToolAnnotations = ToolAnnotations
types_mod.CallToolResult = CallToolResult; types_mod.TextContent = TextContent
mcp.server = server_mod; mcp.types = types_mod
sys.modules['mcp'] = mcp; sys.modules['mcp.server'] = server_mod; sys.modules['mcp.types'] = types_mod
spec = importlib.util.spec_from_file_location('knowledge_mcp_dispatch', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
runtime = json.loads(open(sys.argv[2], encoding='utf-8').read())
adapter = module.create_server(runtime)
if sys.argv[3] == '__list__':
    print(json.dumps([tool.name for tool in asyncio.run(adapter.list_tools())]))
else:
    result = asyncio.run(adapter.call_tool(sys.argv[3], json.loads(sys.argv[4])))
    print(json.dumps(result.structuredContent, ensure_ascii=False))
`);
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };
  delete env.PYTHONPATH; delete env.PYTHONHOME;
  const dispatch = (tool, args = {}) => new Promise((resolve, reject) => {
    const child = spawn(mcpPython, [driver, mcpScript, runtimeFile, tool, JSON.stringify(args)], { cwd: root, env });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', code => {
      if (code !== 0) return reject(Error('MCP driver failed: ' + stderr + stdout));
      try { resolve(JSON.parse(stdout.trim().split('\n').at(-1))); }
      catch { reject(Error('MCP driver returned unreadable output: ' + stdout + stderr)); }
    });
  });
  try {
    const names = await dispatch('__list__');
    assert.ok(names.includes('knowledge_adopted_source'));
    assert.ok(names.includes('knowledge_adopted_image'));
    assert.ok(names.every(name => !/ledger|approval|compliant/i.test(name)));
    service.control.noNormalized = true;
    const read = await dispatch('knowledge_adopted_read', { kind: 'source', sourceId: 'source-1' });
    assert.equal(read.status, 'FAILED');
    assert.equal(read.error_code, 'ADOPTED_SOURCE_CONTENT_UNAVAILABLE');
    assert.match(read.message, /knowledge_adopted_source/);
    const source = await dispatch('knowledge_adopted_source', { sourceId: 'source-1' });
    assert.equal(source.status, 'READY');
    assert.equal(source.bytesSha256, service.originalRevision);
    assert.deepEqual(await readFile(source.path), service.originalBytes);
    const image = await dispatch('knowledge_adopted_image', { imageId: 'image-1' });
    assert.equal(image.status, 'READY');
    assert.equal(image.bytesSha256, digest(service.imageBytes));
    assert.equal(image.locator.location.page_number, null);
    const consumed = await bridge.loadConsumption('media-task');
    assert.deepEqual(consumed.facts, {});
    assert.deepEqual(consumed.receipts, []);
    const receipts = JSON.parse(await readFile(bridge.mediaReceiptPath('media-task'), 'utf8'));
    assert.deepEqual(receipts.entries.map(entry => entry.tool), ['adopted_source', 'adopted_image']);
  } finally { await bridge.close(); await rm(root, { recursive: true, force: true }); }
});
