/** Task-bound PAIR knowledge consumer. PAIR owns originals; ReqPilotX owns bindings. */
import { createHash, createHmac, randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile, rename, chmod } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';

const BINDING_SCHEMA = 'reqpilot-task-evidence/v1';
const CONSUMPTION_SCHEMA = 'reqpilot-pair-evidence-consumption/v1';
const REGISTRATION_SCHEMA = 'reqpilot-pair-evidence-registration/v1';
const MODES = ['NONE', 'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED'];
const PROJECT_KINDS = new Set(['REFERENCE', 'PROJECT_EVIDENCE']);
const SAFE = /^[a-zA-Z0-9_.:-]{1,200}$/;
const sha = value => createHash('sha256').update(value).digest('hex');
const stable = value => Array.isArray(value)
  ? '[' + value.map(stable).join(',') + ']'
  : value && typeof value === 'object'
    ? '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}'
    : JSON.stringify(value);
const digest = value => sha(stable(value));
const fileKey = value => sha(String(value)).slice(0, 32);
const bounded = (value, maximum = 2000) => typeof value === 'string' ? value.slice(0, maximum) : '';

export class KnowledgeError extends Error {
  constructor(code, status = 409, detail) {
    super(detail ? `${code}: ${detail}` : code);
    this.code = code; this.status = status;
  }
}

function roleFields(value) {
  if (!value || typeof value !== 'object') return roleFor('REFERENCE');
  return { authority: value.authority || 'UNKNOWN', claimRole: value.claimRole || 'REFERENCE_CONTEXT',
    applicability: value.applicability || 'REFERENCE', label: value.label || '' };
}
function roleFor(kind, authoritative = false, label = '') {
  if (kind === 'PROJECT_EVIDENCE' || authoritative) return {
    authority: 'INTERNAL_CONTROLLED', claimRole: 'PROJECT_RECORD',
    applicability: 'APPLICABLE', label: label || '已声明内部依据' };
  return { authority: 'UNKNOWN', claimRole: 'REFERENCE_CONTEXT',
    applicability: 'REFERENCE', label: label || '参考上下文' };
}

function jobSummary(job) {
  return job ? { id: String(job.id || ''), state: String(job.state || ''),
    message: String(job.message || ''), updatedAt: String(job.updatedAt || '') } : null;
}
function latestJob(jobs, documentId) {
  return (jobs || []).filter(job => job.documentId === documentId)
    .sort((left, right) => String(right.updatedAt).localeCompare(String(left.updatedAt)))[0] || null;
}
function documentSummary(document, job = null) {
  const pages = Array.isArray(document?.pages) ? document.pages.length : 0;
  const expected = Number.isInteger(document?.expectedPages) ? document.expectedPages : null;
  const failed = ['failed', 'cancelled'].includes(job?.state);
  return {
    id: String(document?.id || ''), kbId: String(document?.kbId || ''),
    name: String(document?.name || ''), mime: String(document?.mime || ''),
    state: document?.state || 'deleted', pages, expectedPages: expected,
    coverage: failed ? (pages ? 'partial' : 'unknown') : document?.state === 'extracting' ? 'partial' : 'complete',
    extractionRevision: document?.backendRevision || '',
    originalRevision: String(document?.sha256 || ''),
    wiki: document?.wikiState || 'not-requested',
    createdAt: document?.createdAt || '', job: jobSummary(job),
  };
}

function topicMetadata(topic) {
  const availability = topic?.availability;
  const verification = topic?.verification;
  const applicability = topic?.applicability;
  if (!topic || typeof topic !== 'object' || typeof topic.versionId !== 'string'
    || typeof topic.kbId !== 'string' || typeof topic.topicId !== 'string'
    || typeof topic.title !== 'string' || !['ai-organized', 'human-authored'].includes(topic.origin)
    || !Array.isArray(topic.authors) || topic.authors.some(item => typeof item !== 'string')
    || !availability || !['available', 'draft'].includes(availability.state)
    || !verification || !['verified', 'pending'].includes(verification.state)
    || !applicability || !['known', 'unknown'].includes(applicability.state)) {
    throw new KnowledgeError('TEAM_TOPIC_METADATA_INVALID', 502);
  }
  return { kbId: topic.kbId, versionId: topic.versionId, topicId: topic.topicId,
    title: topic.title, availability, verification, origin: topic.origin,
    applicability, authors: topic.authors };
}

function topicSourceMetadata(source) {
  if (!source || typeof source !== 'object' || typeof source.documentId !== 'string'
    || typeof source.title !== 'string' || !/^[0-9a-f]{64}$/.test(source.originalRevision || '')
    || !/^[0-9a-f]{64}$/.test(source.extractionRevision || '')
    || !['complete', 'partial', 'unknown'].includes(source.coverage)
    || !source.location || !['physical-page', 'logical-section'].includes(source.location.kind)
    || !Number.isSafeInteger(source.location.number) || source.location.number < 1
    || typeof source.text !== 'string' || !source.text.trim()) {
    throw new KnowledgeError('TEAM_TOPIC_SOURCE_INVALID', 502);
  }
  return { documentId: source.documentId, title: source.title,
    originalRevision: source.originalRevision, extractionRevision: source.extractionRevision,
    coverage: source.coverage,
    location: { kind: source.location.kind, number: source.location.number } };
}

export class KnowledgeBridge {
  constructor({ service, runtime, dataRoot, applicationId, serverFactory = createServer,
    toolScript = new URL('./knowledge-mcp.py', import.meta.url), records = new Map() } = {}) {
    this.service = service || null; this.runtime = runtime || null;
    this.dataRoot = dataRoot; this.applicationId = applicationId || null;
    this.serverFactory = serverFactory; this.toolScript = toolScript;
    this.records = records; this.pending = new Map(); this.server = null; this.serverState = null;
    this.serverPromise = null; this.closing = false;
    this.tokens = new Map();
  }
  appId() {
    const value = typeof this.applicationId === 'function' ? this.applicationId() : this.applicationId;
    return typeof value === 'string' && value ? value : null;
  }
  available() { return Boolean(this.service && this.appId() && this.dataRoot); }
  api() {
    if (!this.available()) throw new KnowledgeError('KNOWLEDGE_SERVICE_UNAVAILABLE', 503);
    return this.service;
  }
  getRecord(commandId) { return this.records.get(commandId) || null; }
  registerRecord(record) { if (record?.commandId) this.records.set(record.commandId, record); }
  bindingPath(commandId) { return join(this.dataRoot, 'knowledge-bindings', commandId + '.binding.json'); }
  runtimePath(commandId) { return join(this.dataRoot, 'knowledge-bindings', commandId + '.runtime.json'); }
  consumptionPath(commandId) { return join(this.dataRoot, 'knowledge-consumption', commandId + '.json'); }
  metadataPath(projectId) { return join(this.dataRoot, 'knowledge-project-metadata', fileKey(projectId) + '.json'); }
  async writeJson(path, value, mode = 0o600) {
    await mkdir(join(path, '..'), { recursive: true, mode: 0o700 });
    const temporary = path + '.' + randomBytes(6).toString('hex') + '.tmp';
    await writeFile(temporary, JSON.stringify(value, null, 2) + '\n', { mode });
    await rename(temporary, path); await chmod(path, mode);
  }
  async readJson(path) { return JSON.parse(await readFile(path, 'utf8')); }
  owner(projectId) { return { applicationId: this.appId(), projectId }; }
  async projectLibrary(projectId) {
    const api = this.api();
    const owner = this.owner(projectId);
    const existing = api.getProjectReferenceLibrary(owner);
    if (existing) {
      if (existing.owner?.kind !== 'application-project'
        || existing.owner?.applicationId !== owner.applicationId
        || existing.owner?.projectId !== owner.projectId)
        throw new KnowledgeError('PROJECT_LIBRARY_UNVERIFIED');
      return existing;
    }
    const created = await api.ensureProjectReferenceLibrary(owner, 'ReqPilotX 项目参考 · ' + projectId);
    const readBack = api.getProjectReferenceLibrary(owner);
    if (!created?.id || !readBack?.id || created.id !== readBack.id)
      throw new KnowledgeError('PROJECT_LIBRARY_UNVERIFIED');
    return readBack;
  }
  async projectMetadata(projectId) {
    try { return await this.readJson(this.metadataPath(projectId)); }
    catch (error) { if (error.code === 'ENOENT') return { schema: 'reqpilot-project-reference-metadata/v1', projectId, documents: {} }; throw error; }
  }
  async updateProjectMetadata(projectId, documentId, value) {
    const metadata = await this.projectMetadata(projectId);
    metadata.documents[documentId] = value;
    await this.writeJson(this.metadataPath(projectId), metadata);
  }
  async projectDocuments(projectId) {
    const library = await this.projectLibrary(projectId);
    const metadata = await this.projectMetadata(projectId);
    const snapshot = this.api().snapshot();
    return (snapshot.documents || []).filter(item => item.kbId === library.id && item.state !== 'deleted')
      .map(item => ({ ...documentSummary(item, latestJob(snapshot.jobs, item.id)),
        role: metadata.documents?.[item.id] || roleFor('REFERENCE') }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }
  async inventory(projectId) {
    if (!this.available()) return { available: false, reason: 'KNOWLEDGE_SERVICE_UNAVAILABLE' };
    const snapshot = this.api().snapshot();
    const library = await this.projectLibrary(projectId);
    const teamLibraries = (snapshot.bases || []).filter(base => !base.owner).map(base => ({
      id: base.id, name: base.name, documents: (snapshot.documents || []).filter(item => item.kbId === base.id && item.state !== 'deleted').map(documentSummary) }));
    return { available: true, applicationId: this.appId(), projectLibrary: library, projectDocuments: await this.projectDocuments(projectId), teamLibraries };
  }
  async uploadProjectReference(projectId, name, bytes, kind = 'REFERENCE') {
    if (typeof name !== 'string' || !name.trim() || Buffer.byteLength(name) > 240
      || /[\\/\0]/.test(name)) throw new KnowledgeError('INVALID_REFERENCE_NAME', 422);
    if (!PROJECT_KINDS.has(kind)) throw new KnowledgeError('INVALID_REFERENCE_KIND', 422);
    const library = await this.projectLibrary(projectId);
    const imported = await this.api().importProjectReferenceSource(library.id, name, new Uint8Array(bytes));
    await this.updateProjectMetadata(projectId, imported.document.id, {
      kind, ...roleFor(kind, kind === 'PROJECT_EVIDENCE', name) });
    return { document: documentSummary(imported.document, imported.job), job: imported.job,
      sourceReady: imported.sourceReady, wiki: imported.wiki };
  }
  async retryProjectReference(projectId, documentId) {
    const library = await this.projectLibrary(projectId);
    const snapshot = this.api().snapshot();
    const document = (snapshot.documents || []).find(item => item.id === documentId && item.kbId === library.id && item.state !== 'deleted');
    if (!document) throw new KnowledgeError('REFERENCE_NOT_FOUND', 404);
    const original = await this.api().original(documentId);
    const imported = await this.api().importProjectReferenceSource(library.id, document.name, new Uint8Array(original.bytes));
    return { document: documentSummary(imported.document, imported.job), job: imported.job, sourceReady: imported.sourceReady, wiki: imported.wiki };
  }
  async teamInventory() {
    if (!this.available()) return [];
    const snapshot = this.api().snapshot();
    return (snapshot.bases || []).filter(base => !base.owner).map(base => ({
      id: base.id, name: base.name,
      documents: (snapshot.documents || []).filter(item => item.kbId === base.id && item.state !== 'deleted')
        .map(item => documentSummary(item, latestJob(snapshot.jobs, item.id))) }));
  }
  async teamTopics(kbId) {
    const snapshot = this.api().snapshot();
    const base = (snapshot.bases || []).find(item => item.id === kbId);
    if (!base || base.owner) throw new KnowledgeError('TEAM_LIBRARY_INVALID');
    const caller = 'reqpilotx-pair:ui:' + this.appId() + ':' + kbId;
    await this.api().select(caller, [kbId]);
    const versions = await this.api().topicVersions(caller, kbId);
    return (versions || []).map(topicMetadata);
  }
  async prepareBinding({ commandId, projectId, mode, projectDocumentIds = [], team = null }) {
    if (!SAFE.test(commandId) || !SAFE.test(projectId) || !MODES.includes(mode))
      throw new KnowledgeError('INVALID_EVIDENCE_SELECTION', 422);
    if (mode === 'NONE' && (projectDocumentIds.length || team)) throw new KnowledgeError('INVALID_EVIDENCE_SELECTION', 422);
    if (mode === 'TEAM_KNOWLEDGE' && projectDocumentIds.length) throw new KnowledgeError('INVALID_EVIDENCE_SELECTION', 422);
    if (['PROJECT_REFERENCES', 'COMBINED'].includes(mode) && !projectDocumentIds.length)
      throw new KnowledgeError('PROJECT_DOCUMENT_SELECTION_REQUIRED', 422);
    if (['TEAM_KNOWLEDGE', 'COMBINED'].includes(mode) && !team) throw new KnowledgeError('TEAM_SELECTION_REQUIRED', 422);
    const selection = { mode, projectDocumentIds: [...new Set(projectDocumentIds)].sort(),
      team: team ? { kbId: String(team.kbId || ''), documents: [...new Set(team.documents || [])].sort(),
        topicVersionId: team.topicVersionId || null, authoritative: team.authoritative === true } : null };
    let existing = null;
    try { existing = await this.readJson(this.bindingPath(commandId)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (existing) {
      if (digest(existing.selection) !== digest(selection)) throw new KnowledgeError('KNOWLEDGE_BINDING_CONFLICT');
      return existing.binding;
    }
    const api = this.api();
    const binding = { schema_version: BINDING_SCHEMA, mode, application_id: this.appId(),
      project_id: projectId, created_at: new Date().toISOString(), project: null, team: null };
    if (mode !== 'NONE') {
      const snapshot = api.snapshot();
      if (selection.projectDocumentIds.length || mode === 'PROJECT_REFERENCES' || mode === 'COMBINED') {
        const library = await this.projectLibrary(projectId);
        const metadata = await this.projectMetadata(projectId);
        const available = new Set((snapshot.documents || []).filter(item => item.kbId === library.id && item.state !== 'deleted').map(item => item.id));
        if (!selection.projectDocumentIds.length || selection.projectDocumentIds.some(id => !available.has(id)))
          throw new KnowledgeError('PROJECT_REFERENCE_UNAVAILABLE', 422);
        const caller = 'reqpilotx-task:' + this.appId() + ':' + commandId + ':project';
        await api.select(caller, [library.id]);
        const captured = await api.captureSelectedSources(caller,
          selection.projectDocumentIds.map(documentId => ({ kbId: library.id, documentId })));
        binding.project = { library_id: library.id, caller,
          documents: captured.documents.map(item => ({
            kbId: item.kbId, documentId: item.documentId, name: item.name, mime: item.mime,
            originalRevision: item.originalRevision, extractionRevision: item.extractionRevision,
            coverage: item.coverage, location: item.location, pages: item.pages,
            expectedPages: item.expectedPages, wiki: item.wiki })),
          roles: Object.fromEntries(selection.projectDocumentIds.map(id => [id, roleFields(metadata.documents?.[id])])) };
      }
      if (selection.team) {
        const base = (snapshot.bases || []).find(item => item.id === selection.team.kbId);
        if (!base || base.owner) throw new KnowledgeError('TEAM_LIBRARY_INVALID');
        const caller = 'reqpilotx-task:' + this.appId() + ':' + commandId + ':team';
        await api.select(caller, [base.id]);
        if (selection.team.topicVersionId) {
          const versions = (await api.topicVersions(caller, base.id)).map(topicMetadata);
          const version = versions.find(item => item.versionId === selection.team.topicVersionId);
          if (!version) throw new KnowledgeError('TEAM_TOPIC_VERSION_UNAVAILABLE', 422);
          if (version.availability.state !== 'available') throw new KnowledgeError('TEAM_TOPIC_DRAFT', 422);
          await api.selectTopicVersion(caller, base.id, version.versionId);
          const selectionPin = api.topicSelection(caller);
          if (!selectionPin || selectionPin.kbId !== base.id || selectionPin.versionId !== version.versionId)
            throw new KnowledgeError('TEAM_TOPIC_NOT_SELECTED');
          const read = await api.readTopicVersion(caller, base.id, version.versionId);
          if (read.versionId !== version.versionId || read.kbId !== base.id
            || read.topicId !== version.topicId || read.origin !== version.origin
            || read.availability.state !== 'available' || typeof read.topic?.revision !== 'string'
            || !/^[0-9a-f]{64}$/.test(read.topic.revision)) {
            throw new KnowledgeError('TEAM_TOPIC_READ_SHAPE_INVALID', 502);
          }
          const sources = read.sources.map(topicSourceMetadata);
          binding.team = { kind: 'TOPIC', library_id: base.id, caller,
            topic: { version_id: version.versionId, topic_id: version.topicId, title: version.title,
              availability: version.availability, verification: version.verification,
              origin: version.origin, applicability: version.applicability,
              authors: version.authors, topic_revision: read.topic.revision,
              sources_digest: digest(sources), sources },
            roles: { [version.versionId]: roleFor('REFERENCE', selection.team.authoritative, version.title || base.name) } };
        } else {
          const pin = api.topicSelection(caller);
          if (pin) await api.selectTopicVersion(caller, base.id, null);
          if (!selection.team.documents.length) throw new KnowledgeError('TEAM_DOCUMENT_SELECTION_REQUIRED', 422);
          const available = new Set((snapshot.documents || []).filter(item => item.kbId === base.id && item.state !== 'deleted').map(item => item.id));
          if (selection.team.documents.some(id => !available.has(id))) throw new KnowledgeError('TEAM_DOCUMENT_UNAVAILABLE', 422);
          const captured = await api.captureSelectedSources(caller,
            selection.team.documents.map(documentId => ({ kbId: base.id, documentId })));
          binding.team = { kind: 'LIVE', library_id: base.id, caller,
            documents: captured.documents.map(item => ({
              kbId: item.kbId, documentId: item.documentId, name: item.name, mime: item.mime,
              originalRevision: item.originalRevision, extractionRevision: item.extractionRevision,
              coverage: item.coverage, location: item.location, pages: item.pages,
              expectedPages: item.expectedPages, wiki: item.wiki })),
            roles: Object.fromEntries(selection.team.documents.map(id => [id, roleFor('REFERENCE', selection.team.authoritative)])) };
        }
      }
    }
    binding.binding_sha256 = digest(binding);
    const expected = { NONE: [false, false], PROJECT_REFERENCES: [true, false], TEAM_KNOWLEDGE: [false, true], COMBINED: [true, true] }[mode];
    if (JSON.stringify([Boolean(binding.project?.documents?.length), Boolean(binding.team?.documents?.length || binding.team?.topic)])
      !== JSON.stringify(expected)) throw new KnowledgeError('INVALID_EVIDENCE_SELECTION', 422);
    await this.writeJson(this.bindingPath(commandId), { schema: 'reqpilot-pair-knowledge-binding-record/v1', selection, binding });
    return binding;
  }
  async loadBinding(commandId) {
    try { return (await this.readJson(this.bindingPath(commandId))).binding; }
    catch (error) { if (error.code === 'ENOENT') throw new KnowledgeError('KNOWLEDGE_BINDING_MISSING', 409); throw error; }
  }
  async checkBinding(commandId) {
    const binding = await this.loadBinding(commandId);
    if (binding.mode === 'NONE') return { state: 'CURRENT', message: '' };
    let api;
    try {
      api = this.api();
      if (binding.project) {
        const captured = await api.captureSelectedSources(binding.project.caller,
          binding.project.documents.map(item => ({ kbId: item.kbId, documentId: item.documentId })));
        if (digest(captured.documents) !== digest(binding.project.documents))
          return { state: 'CHANGED', message: '项目参考的原文或解析版本已变化' };
      }
      if (binding.team?.kind === 'LIVE') {
        const captured = await api.captureSelectedSources(binding.team.caller,
          binding.team.documents.map(item => ({ kbId: item.kbId, documentId: item.documentId })));
        if (digest(captured.documents) !== digest(binding.team.documents))
          return { state: 'CHANGED', message: '团队文档的原文或解析版本已变化' };
      } else if (binding.team?.kind === 'TOPIC') {
        const pin = api.topicSelection(binding.team.caller);
        if (!pin) return { state: 'CHANGED', message: '保留主题选择已不在当前任务范围' };
        const read = await api.readTopicVersion(binding.team.caller, binding.team.library_id,
          binding.team.topic.version_id);
        const sources = read.sources.map(topicSourceMetadata);
        if (read.versionId !== binding.team.topic.version_id
          || read.topicId !== binding.team.topic.topic_id
          || read.topic?.revision !== binding.team.topic.topic_revision
          || digest(sources) !== binding.team.topic.sources_digest)
          return { state: 'CHANGED', message: '保留主题版本的原文或摘录已变化' };
      }
      return { state: 'CURRENT', message: '' };
    } catch (error) {
      return { state: 'UNAVAILABLE', message: String(error?.message || error) };
    }
  }
  async markStatus(commandId, state, message) {
    const record = this.getRecord(commandId);
    if (!record || !this.runtime) return false;
    try {
      const workspace = record.prepared?.workspace || join(this.runtime.dataRoot, record.workspaceRelative || '');
      const capabilityRoot = record.capabilityRoot;
      if (!workspace || !capabilityRoot) return false;
      const request = { task_id: record.prepared?.task_id, state, message,
        binding_sha256: (await this.loadBinding(commandId)).binding_sha256 };
      const file = join(this.runtime.dataRoot, 'knowledge-status-' + commandId + '.json');
      await writeFile(file, JSON.stringify(request), { mode: 0o600 });
      await this.runtime.command([record.python, '-I', join(capabilityRoot, 'scripts/reqpilot.py'),
        'task', 'evidence_status', '--working-root', workspace, '--input', file],
        { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined },
        { signal: AbortSignal.timeout(60000) });
      return true;
    } catch { /* The original evidence refusal remains authoritative. */ }
    return false;
  }
  async requireCurrent(commandId, tool) {
    const status = await this.checkBinding(commandId);
    if (status.state !== 'CURRENT') {
      await this.markStatus(commandId, status.state, status.message);
      throw new KnowledgeError('KNOWLEDGE_EVIDENCE_' + status.state, 409, status.message);
    }
    return status;
  }
  consumption(commandId) {
    if (!this.pending.has(commandId)) this.pending.set(commandId, Promise.resolve());
    return this.pending.get(commandId);
  }
  async loadConsumption(commandId) {
    try { return await this.readJson(this.consumptionPath(commandId)); }
    catch (error) { if (error.code === 'ENOENT') return { schema_version: CONSUMPTION_SCHEMA, facts: {}, receipts: [] }; throw error; }
  }
  async recordConsumption(commandId, tool, request, facts) {
    const run = this.consumption(commandId).then(async () => {
      const binding = await this.loadBinding(commandId);
      const current = await this.loadConsumption(commandId);
      if (current.binding_sha256 && current.binding_sha256 !== binding.binding_sha256)
        throw new KnowledgeError('KNOWLEDGE_BINDING_CONFLICT');
      const ids = facts.map(fact => fact.fact_id);
      for (const fact of facts) current.facts[fact.fact_id] = fact;
      current.schema_version = CONSUMPTION_SCHEMA; current.binding_sha256 = binding.binding_sha256;
      current.receipts.push({ tool, request_sha256: digest(request), returned_at: new Date().toISOString(), fact_ids: ids });
      await this.writeJson(this.consumptionPath(commandId), current);
      return current;
    });
    this.pending.set(commandId, run.catch(() => {}));
    return run;
  }
  catalogDocuments(binding) {
    const liveDocument = (document, kbId, role) => ({
      id: document.documentId, kb_id: kbId, project_id: binding.project_id,
      file_name: document.name, source_sha256: document.originalRevision,
      doc_version: document.extractionRevision.slice(0, 12), status: document.state || 'indexed',
      extractionRevision: document.extractionRevision, location: document.location,
      coverage: document.coverage, authority: role.authority, claim_role: role.claimRole,
      applicability: role.applicability, eligible: true });
    const project = (binding.project?.documents || []).map(document => liveDocument(document,
      binding.project.library_id, binding.project.roles?.[document.documentId] || roleFor('REFERENCE')));
    const teamLive = (binding.team?.documents || []).map(document => liveDocument(document,
      binding.team.library_id, binding.team.roles?.[document.documentId] || roleFor('REFERENCE')));
    const topic = [];
    if (binding.team?.kind === 'TOPIC') {
      const role = binding.team.roles?.[binding.team.topic.version_id] || roleFor('REFERENCE');
      const applicability = binding.team.topic.applicability.state === 'known'
        ? binding.team.topic.applicability.description : role.applicability;
      for (const source of binding.team.topic.sources) {
        topic.push({ id: source.documentId, kb_id: binding.team.library_id, project_id: binding.project_id,
          file_name: source.title, source_sha256: source.originalRevision,
          doc_version: source.extractionRevision.slice(0, 12), status: 'ACTIVE',
          extractionRevision: source.extractionRevision, location: source.location.kind,
          coverage: source.coverage, authority: role.authority, claim_role: role.claimRole,
          applicability, eligible: true, topic_version_id: binding.team.topic.version_id,
          topic_id: binding.team.topic.topic_id, topic_origin: binding.team.topic.origin,
          topic_availability: binding.team.topic.availability,
          topic_verification: binding.team.topic.verification });
      }
    }
    return [...project, ...teamLive, ...topic];
  }
  topicFact(commandId, binding, source, text = source.text) {
    const topic = binding.team.topic;
    const role = binding.team.roles?.[topic.version_id] || roleFor('REFERENCE');
    const verified = role.authority === 'INTERNAL_CONTROLLED' && topic.verification.state === 'verified';
    const applicability = topic.applicability.state === 'known'
      ? topic.applicability.description : role.applicability;
    const location = { kind: source.location.kind, number: source.location.number };
    const id = 'pair-fact-' + digest({ binding: binding.binding_sha256,
      source: topicSourceMetadata(source), location, text }).slice(0, 32);
    return { fact_id: id, document_id: source.documentId, kb_id: binding.team.library_id,
      project_id: binding.project_id, source_sha256: source.originalRevision,
      role: 'PROJECT_EVIDENCE', text, text_sha256: sha(text),
      source_ref: { kind: source.location.kind,
        location: { page_number: source.location.kind === 'physical-page' ? source.location.number : null,
          section: source.location.kind === 'logical-section' ? source.location.number : null,
          number: source.location.number },
        document_id: source.documentId, coverage: source.coverage },
      document_name: source.title, document_version: source.extractionRevision.slice(0, 12),
      authority: verified ? role.authority : (topic.verification.state === 'verified' ? 'TOPIC_VERIFIED_NO_ACCEPTANCE' : 'UNKNOWN'),
      claim_role: role.claimRole, applicability, eligible: true, authority_verified: verified,
      capture_mode: 'PAIR_RETAINED_TOPIC', origin: 'PAIR_RETAINED_TOPIC',
      original_revision: source.originalRevision, extraction_revision: source.extractionRevision,
      coverage: source.coverage, location: source.location.kind,
      topic_version_id: topic.version_id, topic_id: topic.topic_id,
      topic_origin: topic.origin, topic_availability: topic.availability,
      topic_verification: topic.verification,
      url: '/reqpilotx/knowledge/tasks/' + encodeURIComponent(commandId)
        + '/documents/' + encodeURIComponent(source.documentId)
        + '?version=' + encodeURIComponent(topic.version_id) };
  }
  fact(commandId, binding, document, text, location, origin) {
    const id = 'pair-fact-' + digest({ binding: binding.binding_sha256, document: document.id,
      revision: document.source_sha256 || document.extractionRevision, location, text }).slice(0, 32);
    const meta = document.authority ? document : null;
    const authority = meta?.authority || 'UNKNOWN';
    return { fact_id: id, document_id: document.id, kb_id: document.kb_id, project_id: binding.project_id,
      source_sha256: document.source_sha256, role: 'PROJECT_EVIDENCE', text,
      text_sha256: sha(text), source_ref: { kind: location.kind || 'document',
        location: { page_number: location.page || null, section: location.section || null },
        document_id: document.id, coverage: location.coverage || 'complete' },
      document_name: document.file_name, document_version: document.doc_version,
      authority, claim_role: document.claim_role, applicability: document.applicability,
      eligible: true, authority_verified: Boolean(authority && authority !== 'UNKNOWN' && !String(authority).startsWith('REFERENCE')),
      capture_mode: 'PAIR_SELECTED_EVIDENCE', origin,
      original_revision: document.source_sha256,
      extraction_revision: document.extractionRevision || '',
      coverage: location.coverage || 'complete', location: location.kind || 'document',
      url: '/reqpilotx/knowledge/tasks/' + encodeURIComponent(commandId) + '/documents/' + encodeURIComponent(document.id)
        + (binding.team?.kind === 'TOPIC' ? '?version=' + encodeURIComponent(binding.team.topic.version_id) : '') };
  }
  mapLiveHit(commandId, binding, document, hit) {
    return this.fact(commandId, binding, document, hit.text,
      { kind: hit.location?.kind || document.location, page: hit.location?.number,
        coverage: hit.coverage || document.coverage }, 'PAIR_SELECTED_SOURCE');
  }
  async search(commandId, query, limit = 8) {
    if (typeof query !== 'string' || !query.trim() || !Number.isInteger(limit) || limit < 1 || limit > 20)
      throw new KnowledgeError('INVALID_EVIDENCE_QUERY', 422);
    await this.requireCurrent(commandId, 'search');
    const binding = await this.loadBinding(commandId);
    const api = this.api();
    const documents = this.catalogDocuments(binding);
    const byId = new Map(documents.map(item => [item.id, item]));
    let facts = [];
    if (binding.project) {
      for (const item of binding.project.documents) {
        const selection = { version: 1, caller: binding.project.caller, kbIds: [binding.project.library_id],
          documents: binding.project.documents };
        const hits = await api.searchSelectedSources(binding.project.caller, selection, query, limit);
        facts.push(...hits.map(hit => this.mapLiveHit(commandId, binding,
          byId.get(hit.documentId) || documents.find(d => d.id === hit.documentId), hit)));
      }
    }
    if (binding.team?.kind === 'LIVE') {
      const selection = { version: 1, caller: binding.team.caller, kbIds: [binding.team.library_id], documents: binding.team.documents };
      const hits = await api.searchSelectedSources(binding.team.caller, selection, query, limit);
      facts.push(...hits.map(hit => this.mapLiveHit(commandId, binding, byId.get(hit.documentId) || documents.find(d => d.id === hit.documentId), hit)));
    } else if (binding.team?.kind === 'TOPIC') {
      const read = await api.readTopicVersion(binding.team.caller, binding.team.library_id, binding.team.topic.version_id);
      const tokens = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
      facts.push(...read.sources.filter(source => tokens.every(token =>
        source.text.toLocaleLowerCase().includes(token)) || tokens.some(token =>
        source.text.toLocaleLowerCase().includes(token)))
        .map(source => this.topicFact(commandId, binding, source)));
    }
    facts = [...new Map(facts.map(item => [item.fact_id, item])).values()].slice(0, limit);
    await this.recordConsumption(commandId, 'search', { query, limit }, facts);
    return { bindingSha256: binding.binding_sha256, query, facts,
      coverage: facts.map(item => ({ documentId: item.document_id, coverage: item.coverage })) };
  }
  async read(commandId, documentId, firstPage = 1, count = 4) {
    if (!SAFE.test(documentId) || !Number.isInteger(firstPage) || firstPage < 1
      || !Number.isInteger(count) || count < 1 || count > 20) throw new KnowledgeError('INVALID_EVIDENCE_READ', 422);
    await this.requireCurrent(commandId, 'read');
    const binding = await this.loadBinding(commandId);
    const api = this.api();
    const documents = this.catalogDocuments(binding);
    let document = null, caller = null;
    if (binding.project?.documents.some(item => item.documentId === documentId)) {
      document = documents.find(item => item.id === documentId); caller = binding.project.caller;
      const read = await api.readSelectedSources(caller,
        { version: 1, caller, kbIds: [binding.project.library_id], documents: binding.project.documents },
        documentId, firstPage, count);
      const facts = read.pages.map(page => this.fact(commandId, binding, document, page.text,
        { kind: document.location, page: page.number, coverage: read.document.coverage }, 'PAIR_SELECTED_SOURCE'));
      await this.recordConsumption(commandId, 'read', { documentId, firstPage, count }, facts);
      return { bindingSha256: binding.binding_sha256, document: documentSummary({ ...read.document, id: documentId,
        kbId: document.kb_id, name: document.file_name, mime: 'application/octet-stream', sha256: document.source_sha256,
        pages: Array(read.pages.length), expectedPages: read.document.expectedPages, state: 'indexed', wikiState: read.document.wiki }),
        pages: read.pages.map(page => ({ number: page.number, url: page.url })), facts, truncated: read.truncated };
    }
    if (binding.team?.kind === 'LIVE' && binding.team.documents.some(item => item.documentId === documentId)) {
      document = documents.find(item => item.id === documentId); caller = binding.team.caller;
      const read = await api.readSelectedSources(caller,
        { version: 1, caller, kbIds: [binding.team.library_id], documents: binding.team.documents },
        documentId, firstPage, count);
      const facts = read.pages.map(page => this.fact(commandId, binding, document, page.text,
        { kind: document.location, page: page.number, coverage: read.document.coverage }, 'PAIR_SELECTED_SOURCE'));
      await this.recordConsumption(commandId, 'read', { documentId, firstPage, count }, facts);
      return { bindingSha256: binding.binding_sha256, document: documentSummary({ ...read.document, id: documentId,
        kbId: document.kb_id, name: document.file_name, mime: 'application/octet-stream', sha256: document.source_sha256,
        pages: Array(read.pages.length), expectedPages: read.document.expectedPages, state: 'indexed', wikiState: read.document.wiki }),
        pages: read.pages.map(page => ({ number: page.number, url: page.url })), facts, truncated: read.truncated };
    }
    if (binding.team?.kind === 'TOPIC') {
      const read = await api.readTopicVersion(binding.team.caller, binding.team.library_id, binding.team.topic.version_id);
      const source = read.sources.find(item => item.documentId === documentId);
      if (!source) throw new KnowledgeError('DOCUMENT_NOT_SELECTED', 409, 'Only documents frozen in this task binding can be read');
      const fact = this.topicFact(commandId, binding, source);
      await this.recordConsumption(commandId, 'read', { documentId, firstPage, count }, [fact]);
      return { bindingSha256: binding.binding_sha256,
        document: { id: source.documentId, kbId: binding.team.library_id, name: source.title,
          mime: 'text/markdown', state: 'indexed', sha256: source.originalRevision,
          pages: source.location.number, expectedPages: source.location.number,
          coverage: source.coverage, extractionRevision: source.extractionRevision,
          wiki: 'not-requested' },
        pages: [{ number: source.location.number, url: fact.url }], facts: [fact], truncated: false };
    }
    throw new KnowledgeError('DOCUMENT_NOT_SELECTED', 409, 'Only documents frozen in this task binding can be read');
  }
  async topicRead(commandId) {
    await this.requireCurrent(commandId, 'topic_read');
    const binding = await this.loadBinding(commandId);
    if (binding.team?.kind !== 'TOPIC') throw new KnowledgeError('RETAINED_TOPIC_NOT_SELECTED', 409);
    const read = await this.api().readTopicVersion(binding.team.caller, binding.team.library_id, binding.team.topic.version_id);
    const facts = read.sources.map(source => this.topicFact(commandId, binding, source));
    await this.recordConsumption(commandId, 'topic_read', {}, facts);
    return { bindingSha256: binding.binding_sha256,
      kbId: read.kbId, topicId: read.topicId, versionId: read.versionId,
      origin: read.origin, authors: read.authors, availability: read.availability,
      verification: read.verification, applicability: read.topic.applicability,
      topic: read.topic, sources: read.sources, topicRead: read, facts };
  }
  async catalog(commandId) {
    const binding = await this.loadBinding(commandId);
    const consumed = await this.loadConsumption(commandId);
    const documents = this.catalogDocuments(binding);
    const catalog = { schema_version: 'project-knowledge-facts/v1', project_id: binding.project_id,
      knowledge_base_version: 1, evidence_catalog: consumed.facts,
      documents, gaps: [], selection: 'EXPLICIT_TASK_EVIDENCE' };
    catalog.catalog_sha256 = digest(catalog);
    const registration = { schema_version: REGISTRATION_SCHEMA, binding_sha256: binding.binding_sha256,
      mode: binding.mode, application_id: binding.application_id, project_id: binding.project_id,
      catalog_sha256: catalog.catalog_sha256, documents,
      facts: Object.keys(catalog.evidence_catalog).sort().map(key => catalog.evidence_catalog[key]),
      receipts: consumed.receipts, authority: 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE',
      human_acceptance: 'NOT_CLAIMED' };
    registration.registration_sha256 = digest(registration);
    return { catalog, registration };
  }
  async writeCatalog(commandId) {
    const record = this.getRecord(commandId);
    if (!record?.prepared?.task_root) throw new KnowledgeError('TASK_NOT_PREPARED', 409);
    const { catalog, registration } = await this.catalog(commandId);
    const directory = join(record.prepared.task_root, 'inputs', 'knowledge');
    await mkdir(directory, { recursive: true, mode: 0o700 });
    await this.writeJson(join(directory, 'knowledge-catalog.json'), catalog, 0o400);
    await this.writeJson(join(directory, 'evidence-registration.json'), registration, 0o400);
    return { catalog, registration };
  }
  async analysis(commandId, decisions, unitIds, expectedRevision, modelCommandId) {
    const record = this.getRecord(commandId);
    if (!record?.prepared?.task_root) throw new KnowledgeError('TASK_NOT_PREPARED', 409);
    if (!Array.isArray(decisions) || !decisions.length) throw new KnowledgeError('INVALID_ANALYSIS', 422);
    if (typeof expectedRevision !== 'string' || !expectedRevision) throw new KnowledgeError('EXPECTED_REVISION_REQUIRED', 422);
    if (typeof modelCommandId !== 'string' || !modelCommandId) throw new KnowledgeError('COMMAND_ID_REQUIRED', 422);
    if (unitIds !== undefined && unitIds !== null && (!Array.isArray(unitIds) || !unitIds.length
      || unitIds.some(item => typeof item !== 'string' || !item) || new Set(unitIds).size !== unitIds.length))
      throw new KnowledgeError('INVALID_ANALYSIS', 422);
    const normalized = decisions.map(item => {
      if (!item || typeof item !== 'object' || typeof item.unit_id !== 'string' || !item.unit_id
        || typeof item.decision !== 'string' || typeof item.reason !== 'string'
        || !Array.isArray(item.evidence)) throw new KnowledgeError('INVALID_ANALYSIS', 422);
      return { unit_id: item.unit_id, decision: item.decision, reason: item.reason,
        evidence: item.evidence.map(citation => {
          if (!citation || typeof citation.fact_id !== 'string' || typeof citation.quote !== 'string')
            throw new KnowledgeError('INVALID_ANALYSIS', 422);
          return { fact_id: citation.fact_id, quote: citation.quote };
        }),
        clarification_question: typeof item.clarification_question === 'string' ? item.clarification_question : '' };
    });
    await this.requireCurrent(commandId, 'analysis');
    const binding = await this.loadBinding(commandId);
    const consumed = await this.loadConsumption(commandId);
    for (const decision of normalized) for (const citation of decision.evidence) {
      const fact = consumed.facts[citation.fact_id];
      if (!fact || !citation.quote.trim() || !fact.text.includes(citation.quote))
        throw new KnowledgeError('EVIDENCE_FACT_NOT_CONSUMED', 409, 'Cite only exact quotes from logged task evidence results');
    }
    const { catalog, registration } = await this.writeCatalog(commandId);
    if (!this.runtime.attestationKey) throw new KnowledgeError('HOST_ATTESTATION_KEY_UNAVAILABLE', 503);
    const payload = { schema: 'reqpilot-pair-analysis-attestation/v1',
      application_id: binding.application_id, project_id: binding.project_id,
      document_id: record.documentId || '', task_id: record.prepared.task_id,
      workbench_ref: record.prepared.work_ref || '',
      binding_sha256: binding.binding_sha256,
      mode: binding.mode, task_revision: expectedRevision, command_id: modelCommandId,
      unit_ids: unitIds || null, decisions: normalized,
      decisions_sha256: digest(normalized), catalog_sha256: catalog.catalog_sha256,
      ledger_sha256: digest({ facts: consumed.facts, receipts: consumed.receipts }),
      consumed_fact_ids: Object.keys(consumed.facts).sort(),
      checked_at: new Date().toISOString() };
    const signature = createHmac('sha256', Buffer.from(this.runtime.attestationKey, 'hex'))
      .update(digest(payload)).digest('hex');
    const attestation = { ...payload, signature };
    const workspace = record.prepared.workspace;
    const directory = join(this.runtime.dataRoot, 'knowledge-analysis');
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const requestFile = join(directory, commandId + '-analysis.json');
    const request = { task_id: record.prepared.task_id, expected_revision: expectedRevision,
      command_id: modelCommandId, decisions: normalized,
      ...(unitIds ? { unit_ids: unitIds } : {}),
      knowledge_attestation: attestation };
    await writeFile(requestFile, JSON.stringify(request), { mode: 0o600 });
    const stdout = await this.runtime.command([record.python, '-I', join(record.capabilityRoot, 'scripts/reqpilot.py'),
      'task', 'analysis', '--working-root', workspace, '--input', requestFile],
      { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined,
        REQPILOT_HOST_ATTESTATION_KEY: this.runtime.attestationKey },
      { signal: AbortSignal.timeout(300000) });
    let result;
    try { result = JSON.parse(stdout); } catch { throw new KnowledgeError('ANALYSIS_RESULT_UNREADABLE', 502); }
    if (result.status === 'FAILED' || result.status === 'UNKNOWN') throw new KnowledgeError(
      result.error_code || 'ANALYSIS_NOT_SAVED', 409, result.message || result.error || result.status);
    return result;
  }
  async original(commandId, documentId, versionId = null) {
    const binding = await this.loadBinding(commandId);
    const project = binding.project?.documents?.find(item => item.documentId === documentId);
    const team = binding.team?.documents?.find(item => item.documentId === documentId);
    if (project || team) return this.api().original(documentId);
    if (binding.team?.kind === 'TOPIC' && versionId === binding.team.topic.version_id) {
      return this.api().topicOriginal(binding.team.library_id, binding.team.topic.version_id, documentId);
    }
    throw new KnowledgeError('DOCUMENT_NOT_SELECTED', 409);
  }
  async ensureServer() {
    if (this.closing) throw new KnowledgeError('KNOWLEDGE_SERVICE_STOPPING', 503);
    if (this.serverState) return this.serverState;
    if (!this.serverPromise) this.serverPromise = this.startServer();
    return this.serverPromise;
  }
  async startServer() {
    let state;
    try {
      state = { server: this.serverFactory(async (req, res) => this.handleEvidence(req, res)) };
      state.server.listen(0, '127.0.0.1');
      await once(state.server, 'listening');
      if (this.closing) {
        await new Promise(resolve => state.server.close(resolve));
        throw new KnowledgeError('KNOWLEDGE_SERVICE_STOPPING', 503);
      }
      state.endpoint = 'http://127.0.0.1:' + state.server.address().port;
      this.serverState = state; this.server = state.server;
      return state;
    } catch (error) {
      this.serverPromise = null; this.serverState = null; this.server = null;
      if (!this.closing && state?.server?.listening) await new Promise(resolve => state.server.close(resolve));
      throw error;
    }
  }
  async runtimeFile(commandId) {
    const state = await this.ensureServer();
    let token = [...this.tokens.entries()].find(([, id]) => id === commandId)?.[0];
    if (!token) {
      token = randomBytes(32).toString('hex'); this.tokens.set(token, commandId);
      const record = { schema: 'reqpilot-knowledge-runtime/v1', endpoint: state.endpoint, token, commandId };
      await this.writeJson(this.runtimePath(commandId), record);
    }
    return this.runtimePath(commandId);
  }
  async mcpBinding(commandId, record) {
    await this.loadBinding(commandId);
    return { command: record.python, args: [fileURLToPath(this.toolScript), await this.runtimeFile(commandId)],
      cwd: record.prepared?.workspace, serverName: 'reqpilot_knowledge' };
  }
  async handleEvidence(req, res) {
    try {
      if (this.closing) {
        res.writeHead(503, { 'content-type': 'application/json' });
        res.end('{"error":"KNOWLEDGE_SERVICE_STOPPING"}');
        return;
      }
      const token = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const commandId = this.tokens.get(token);
      if (!commandId) { res.writeHead(401, { 'content-type': 'application/json' }); res.end('{"error":"UNAUTHORIZED"}'); return; }
      if (req.method !== 'POST') { res.writeHead(405); res.end(); return; }
      let text = ''; for await (const chunk of req) { text += chunk; if (Buffer.byteLength(text) > 262144) { res.writeHead(413); res.end(); return; } }
      const body = JSON.parse(text || '{}');
      let result;
      if (req.url === '/evidence/search') result = await this.search(commandId, body.query, body.limit ?? 8);
      else if (req.url === '/evidence/read') result = await this.read(commandId, body.documentId, body.firstPage ?? 1, body.count ?? 4);
      else if (req.url === '/evidence/topic') result = await this.topicRead(commandId);
      else if (req.url === '/evidence/analysis') result = await this.analysis(commandId, body.decisions,
        body.unitIds, body.expectedRevision, body.commandId);
      else { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify({ ok: true, result }));
    } catch (error) {
      res.writeHead(error?.status || 500, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify({ ok: false, error: { code: error?.code || 'KNOWLEDGE_EVIDENCE_FAILED', message: String(error?.message || error) } }));
    }
  }
  async close() {
    this.closing = true;
    if (this.serverPromise) await this.serverPromise.catch(() => undefined);
    await Promise.allSettled([...this.pending.values()]);
    if (this.server) await new Promise(resolve => this.server.close(resolve));
    this.server = null; this.serverState = null; this.serverPromise = null; this.tokens.clear();
  }
}
