/** Task-bound PAIR knowledge consumer. PAIR owns originals; ReqPilotX owns bindings. */
import { createHash, createHmac, randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile, rename, chmod } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';

const BINDING_SCHEMA = 'reqpilot-task-evidence/v1';
const CONSUMPTION_SCHEMA = 'reqpilot-pair-evidence-consumption/v1';
const MEDIA_RECEIPTS_SCHEMA = 'reqpilot-pair-media-receipts/v1';
const REGISTRATION_SCHEMA = 'reqpilot-pair-evidence-registration/v1';
const MEDIA_MAX_BYTES = 128 * 1024 * 1024;
const MEDIA_RECEIPT_LIMIT = 200;
const MODES = ['NONE', 'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED', 'ADOPTED_KNOWLEDGE'];
const ADOPTED_SECTIONS = ['wiki', 'claims', 'entities', 'relations', 'images', 'experiences', 'conflicts', 'gaps', 'sources'];
const ADOPTED_KINDS = ['wiki', 'source', 'claim', 'entity', 'relation', 'image', 'experience', 'conflict', 'gap'];
const ADOPTED_VERSION = /^[a-f0-9]{64}$/;
const ADOPTED_ID = /^[^\u0000-\u001f\\]{1,300}$/;
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
function safeMediaName(value, fallback) {
  const raw = typeof value === 'string' ? basename(value) : '';
  const name = raw.replace(/[^a-zA-Z0-9._-]+/g, '_').replace(/^[._-]+/, '').slice(0, 120);
  return name || fallback;
}
function mediaMime(value) {
  return typeof value === 'string' && /^[a-zA-Z0-9.+-]+\/[a-zA-Z0-9.+-]+$/.test(value)
    ? value : 'application/octet-stream';
}

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

function adoptedSummary(value) {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string') return null;
  const state = ['ready', 'needs-update', 'blocked'].includes(value.state) ? value.state : 'blocked';
  const versionId = typeof value.versionId === 'string' ? value.versionId : '';
  if (state !== 'blocked' && !ADOPTED_VERSION.test(versionId)) return null;
  if (state === 'blocked' && versionId && !ADOPTED_VERSION.test(versionId)) return null;
  const counts = value.counts && typeof value.counts === 'object' ? value.counts : {};
  return { id: value.id, name: String(value.name || ''), ...(typeof value.purpose === 'string' ? { purpose: value.purpose } : {}),
    operation: String(value.operation || 'connect-existing'), packageVersion: value.packageVersion ?? null,
    okfVersion: value.okfVersion ?? null, versionId, state,
    recommendation: String(value.recommendation || 'rebuild'), addedAt: String(value.addedAt || ''),
    lastCheckedAt: String(value.lastCheckedAt || ''),
    counts: Object.fromEntries(['sources', 'claims', 'entities', 'relations', 'images', 'experiences', 'conflicts', 'gaps', 'wikiPages', 'files']
      .map(key => [key, Number.isSafeInteger(counts[key]) && counts[key] >= 0 ? counts[key] : 0])),
    checks: (Array.isArray(value.checks) ? value.checks : []).slice(0, 32).map(check => ({
      id: String(check?.id || ''), state: ['ok', 'warning', 'problem', 'unknown'].includes(check?.state) ? check.state : 'unknown',
      summary: bounded(check?.summary, 2000), ...(Number.isSafeInteger(check?.count) ? { count: check.count } : {}) })) };
}
function adoptionFailure(error) {
  if (error instanceof KnowledgeError) return error;
  const source = String(error?.code || 'request-failed').replace(/-/g, '_').toUpperCase();
  const status = ['NOT_FOUND', 'SOURCE_UNAVAILABLE'].includes(source) ? 404
    : source === 'PERMISSION_DENIED' ? 403
      : ['LIBRARY_BLOCKED', 'LIBRARY_UNSELECTED', 'VERSION_CHANGED', 'METADATA_ONLY'].includes(source) ? 409
        : ['RESULT_TOO_LARGE'].includes(source) ? 413 : source === 'TIMEOUT' ? 504 : 502;
  return new KnowledgeError('ADOPTED_' + source, status, String(error?.message || error));
}
function adoptedLocator(locator) {
  if (!locator || typeof locator !== 'object') return { kind: 'document', location: { page_number: null, section: null, number: null } };
  const page = locator.kind === 'physical-page' && Number.isSafeInteger(locator.page) && locator.page > 0 ? locator.page : null;
  const section = locator.kind === 'logical-section'
    ? (typeof locator.section === 'string' && locator.section ? locator.section
      : typeof locator.text === 'string' && locator.text ? locator.text : null) : null;
  return { kind: ['physical-page', 'logical-section', 'document', 'image-region'].includes(locator.kind) ? locator.kind : 'document',
    location: { page_number: page, section, number: page } };
}
function adoptedRequest(value) {
  if (!value || typeof value !== 'object' || !ADOPTED_KINDS.includes(value.kind))
    throw new KnowledgeError('INVALID_ADOPTED_READ', 422);
  const safeId = item => typeof item === 'string' && ADOPTED_ID.test(item) && !item.split('/').includes('..');
  if (value.kind === 'wiki') {
    if (!safeId(value.path) || !value.path.endsWith('.md') || value.path.startsWith('/'))
      throw new KnowledgeError('INVALID_ADOPTED_READ', 422);
    return { kind: 'wiki', path: value.path };
  }
  if (value.kind === 'source') {
    if (!safeId(value.sourceId)) throw new KnowledgeError('INVALID_ADOPTED_READ', 422);
    return { kind: 'source', sourceId: value.sourceId };
  }
  if (!safeId(value.id)) throw new KnowledgeError('INVALID_ADOPTED_READ', 422);
  return { kind: value.kind, id: value.id };
}
function adoptedIdentity(item, result) {
  if (result) {
    if (result.kind === 'wiki') return { kind: 'wiki', id: result.path || result.id };
    if (result.kind === 'source') return { kind: 'source', id: result.sourceId || result.id };
    if (ADOPTED_KINDS.includes(result.kind)) return { kind: result.kind, id: result.id };
  }
  const raw = String(item?.evidenceId || '');
  const separator = raw.indexOf(':');
  if (separator < 1) return null;
  const prefix = raw.slice(0, separator), id = raw.slice(separator + 1);
  const kind = prefix === 'conflicts' ? 'conflict' : prefix === 'gaps' ? 'gap' : prefix;
  return ADOPTED_KINDS.includes(kind) && item.kind === kind && id ? { kind: item.kind, id } : null;
}
function adoptedRecordUrl(commandId, kind, id) {
  const query = kind === 'wiki' ? new URLSearchParams({ kind, path: id }) : new URLSearchParams({ kind, id });
  return '/reqpilotx/knowledge/tasks/' + encodeURIComponent(commandId) + '/adopted/records?' + query.toString();
}
function adoptedSourceUrl(commandId, sourceId) {
  return '/reqpilotx/knowledge/tasks/' + encodeURIComponent(commandId) + '/adopted/source/' + encodeURIComponent(sourceId);
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
  adoptionAvailable() {
    return this.available() && typeof this.service.adoptedLibraries === 'function'
      && typeof this.service.bindAdoptedSession === 'function'
      && typeof this.service.adoptedBinding === 'function'
      && typeof this.service.browseAdopted === 'function'
      && typeof this.service.readAdopted === 'function'
      && typeof this.service.adoptedEvidence === 'function'
      && typeof this.service.adoptedSource === 'function'
      && typeof this.service.adoptedImage === 'function';
  }
  adoptedInventory() {
    if (!this.adoptionAvailable()) return { available: false, reason: 'ADOPTION_NOT_SUPPORTED', libraries: [] };
    try {
      const libraries = this.service.adoptedLibraries().map(adoptedSummary).filter(Boolean)
        .sort((left, right) => left.name.localeCompare(right.name));
      return { available: true, libraries };
    } catch (error) { throw adoptionFailure(error); }
  }
  async inspectAdoptedLibrary(libraryId) {
    if (!this.adoptionAvailable() || typeof this.service.inspectAdoptedLibrary !== 'function')
      throw new KnowledgeError('ADOPTION_NOT_SUPPORTED', 503);
    if (!SAFE.test(libraryId || '')) throw new KnowledgeError('INVALID_ADOPTED_LIBRARY', 422);
    try { return adoptedSummary(await this.service.inspectAdoptedLibrary(libraryId)); }
    catch (error) { throw adoptionFailure(error); }
  }
  getRecord(commandId) { return this.records.get(commandId) || null; }
  registerRecord(record) { if (record?.commandId) this.records.set(record.commandId, record); }
  redact(error) {
    const value = String(error?.message || error);
    const key = this.runtime?.attestationKey;
    return key ? value.split(key).join('[redacted]') : value;
  }
  /** Only the fixed isolated actions; the pinned task package is authenticated by task-workflow.py. */
  async taskCommand(record, action, requestFile, fields = {}, { attestation = false, timeout = 60000, workspace = null } = {}) {
    workspace ||= record?.prepared?.workspace;
    if (!record?.python || !workspace || !record.prepared?.task_id || !this.runtime?.command)
      throw new KnowledgeError('TASK_COMMAND_UNAVAILABLE', 503);
    const bundleSha = record.bundleSha || record.prepared.bundle_sha256;
    const sourceSha = record.sourceSha256 || record.prepared.source_sha256;
    if (!/^[a-f0-9]{64}$/.test(bundleSha || '') || !/^[a-f0-9]{64}$/.test(sourceSha || ''))
      throw new KnowledgeError('TASK_COMMAND_BINDING_MISSING', 409);
    const directory = join(this.runtime.dataRoot || this.dataRoot, 'task-commands');
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const file = join(directory, record.commandId + '-' + action + '-' + randomBytes(6).toString('hex') + '.json');
    const payload = { schema: 'reqpilot-pair-host-command/v1', action,
      task_id: record.prepared.task_id, bundle_sha256: bundleSha, source_sha256: sourceSha,
      project_id: record.projectId, document_id: record.documentId,
      work_ref: record.prepared.work_ref || '', expected_revision: fields.expected_revision,
      workflow: record.workflow, request_file: requestFile };
    if (payload.expected_revision === undefined) delete payload.expected_revision;
    if (payload.workflow === undefined) delete payload.workflow;
    await writeFile(file, JSON.stringify(payload), { mode: 0o600 });
    const cacheRoot = join(this.runtime.dataRoot || this.dataRoot, 'task-launches', record.commandId + '-capability');
    const env = { PYTHONPATH: undefined, PYTHONHOME: undefined, REQPILOT_DEVELOPMENT_ROOT: undefined,
      ...(attestation ? { REQPILOT_HOST_ATTESTATION_KEY: this.runtime.attestationKey } : {}) };
    let stdout;
    try {
      stdout = await this.runtime.command([record.python, '-I', fileURLToPath(new URL('./task-workflow.py', import.meta.url)),
        file, workspace, cacheRoot], env, { signal: AbortSignal.timeout(timeout) });
    } catch (error) {
      throw new KnowledgeError('TASK_COMMAND_FAILED', 502, this.redact(error));
    }
    try { return JSON.parse(stdout); }
    catch { throw new KnowledgeError('TASK_COMMAND_RESULT_UNREADABLE', 502, this.redact(stdout).slice(0, 4000)); }
  }
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
  async prepareAdoptedBinding({ commandId, libraryId, versionId, projectId }) {
    if (!SAFE.test(commandId || '') || !SAFE.test(libraryId || '') || !ADOPTED_VERSION.test(versionId || '') || !SAFE.test(projectId || ''))
      throw new KnowledgeError('INVALID_ADOPTED_SELECTION', 422);
    if (!this.adoptionAvailable()) throw new KnowledgeError('ADOPTION_NOT_SUPPORTED', 503);
    const selection = { adopted: { libraryId, versionId } };
    let existing = null;
    try { existing = await this.readJson(this.bindingPath(commandId)); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (existing) {
      if (digest(existing.selection) !== digest(selection)) throw new KnowledgeError('KNOWLEDGE_BINDING_CONFLICT');
      return existing.binding;
    }
    let summary;
    try {
      summary = this.service.adoptedLibraries().map(adoptedSummary).filter(Boolean)
        .find(item => item.id === libraryId);
    } catch (error) { throw adoptionFailure(error); }
    if (!summary) throw new KnowledgeError('ADOPTED_LIBRARY_NOT_FOUND', 404);
    if (summary.state === 'blocked') throw new KnowledgeError('ADOPTED_LIBRARY_BLOCKED', 409,
      'The registered knowledge library has blocking inspection problems.');
    if (summary.versionId !== versionId) throw new KnowledgeError('ADOPTED_VERSION_CHANGED', 409,
      'The selected library version changed; select the exact current version again.');
    const caller = 'reqpilotx-task:' + this.appId() + ':' + commandId + ':adopted';
    let bound, readBack;
    try {
      bound = await this.service.bindAdoptedSession(caller, libraryId, 'connect-existing');
      readBack = this.service.adoptedBinding(caller);
    } catch (error) { throw adoptionFailure(error); }
    if (!readBack || readBack.sessionId !== caller || readBack.libraryId !== libraryId
      || readBack.versionId !== versionId || bound?.versionId !== versionId || bound?.libraryId !== libraryId)
      throw new KnowledgeError('ADOPTED_BINDING_UNCONFIRMED', 409,
        'The Host did not confirm the exact frozen adopted knowledge version.');
    const binding = { schema_version: BINDING_SCHEMA, mode: 'ADOPTED_KNOWLEDGE',
      application_id: this.appId(), project_id: projectId, created_at: new Date().toISOString(),
      project: null, team: null,
      adopted: { library_id: libraryId, version_id: versionId, caller,
        name: summary.name.slice(0, 200), package_version: summary.packageVersion ?? null } };
    binding.binding_sha256 = digest(binding);
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
      if (binding.mode === 'ADOPTED_KNOWLEDGE') {
        const frozen = binding.adopted;
        if (!frozen || typeof frozen.caller !== 'string' || typeof frozen.library_id !== 'string'
          || typeof frozen.version_id !== 'string') return { state: 'INVALID', message: '冻结的已注册知识库绑定无效' };
        const current = api.adoptedBinding?.(frozen.caller);
        if (!current || current.libraryId !== frozen.library_id || current.versionId !== frozen.version_id
          || current.sessionId !== frozen.caller)
          return { state: 'CHANGED', message: '已注册知识库的任务调用身份或冻结版本已变化' };
        const summary = (api.adoptedLibraries?.() || []).map(adoptedSummary).filter(Boolean)
          .find(item => item.id === frozen.library_id);
        if (!summary) return { state: 'UNAVAILABLE', message: '已注册知识库当前不可读取' };
        if (summary.versionId !== frozen.version_id)
          return { state: 'CHANGED', message: '已注册知识库已有新版本；本任务仍冻结原版本，不自动切换' };
        if (summary.state === 'blocked') return { state: 'UNAVAILABLE', message: '已注册知识库当前检查未通过' };
        return { state: 'CURRENT', message: '' };
      }
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
      if (!workspace) return false;
      const request = { task_id: record.prepared?.task_id, state, message,
        binding_sha256: (await this.loadBinding(commandId)).binding_sha256 };
      const file = join(this.runtime.dataRoot,
        'knowledge-status-' + commandId + '-' + randomBytes(16).toString('hex') + '.json');
      await writeFile(file, JSON.stringify(request), { mode: 0o600, flag: 'wx' });
      const result = await this.taskCommand(record, 'evidence_status', file, {}, { timeout: 60000, workspace });
      if (result?.status === 'FAILED') throw new KnowledgeError(
        result.error_code || 'EVIDENCE_STATUS_NOT_SAVED', 409, result.message || result.error || result.status);
      return true;
    } catch (error) {
      /* The original evidence refusal remains authoritative; keep the detail out of protocol output. */
      this.lastStatusError = this.redact(error);
    }
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
  catalogDocuments(binding, consumedFacts = {}) {
    if (binding.mode === 'ADOPTED_KNOWLEDGE') {
      const seen = new Set();
      return Object.values(consumedFacts).filter(fact => fact?.adopted?.library_id === binding.adopted?.library_id)
        .map(fact => {
          if (seen.has(fact.document_id)) return null;
          seen.add(fact.document_id);
          return { id: fact.document_id, kb_id: fact.kb_id, project_id: fact.project_id,
            file_name: fact.document_name || fact.document_id, source_sha256: fact.source_sha256,
            source_sha256_basis: 'CONTENT_REVISION', doc_version: fact.document_version || '',
            status: 'ACTIVE', authority: fact.authority, claim_role: fact.claim_role,
            applicability: fact.applicability, eligible: true,
            adopted_library_id: fact.adopted.library_id, adopted_library_version_id: fact.adopted.version_id,
            content_revision: fact.content_revision, availability: fact.availability,
            ...(fact.source_version ? { source_version: fact.source_version } : {}),
            ...(fact.original_source_version ? { original_source_version: fact.original_source_version } : {}) };
        }).filter(Boolean).sort((left, right) => left.id.localeCompare(right.id));
    }
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
    const kind = location.kind || document.location || 'document';
    const number = Number.isSafeInteger(location.number) ? location.number : null;
    const id = 'pair-fact-' + digest({ binding: binding.binding_sha256, document: document.id,
      revision: document.source_sha256 || document.extractionRevision, location: { kind, number }, text }).slice(0, 32);
    const meta = document.authority ? document : null;
    const authority = meta?.authority || 'UNKNOWN';
    return { fact_id: id, document_id: document.id, kb_id: document.kb_id, project_id: binding.project_id,
      source_sha256: document.source_sha256, role: 'PROJECT_EVIDENCE', text,
      text_sha256: sha(text), source_ref: { kind,
        location: { page_number: kind === 'physical-page' ? number : null,
          section: kind === 'logical-section' ? number : null, number },
        document_id: document.id, coverage: location.coverage || 'complete' },
      document_name: document.file_name, document_version: document.doc_version,
      authority, claim_role: document.claim_role, applicability: document.applicability,
      eligible: true, authority_verified: Boolean(authority && authority !== 'UNKNOWN' && !String(authority).startsWith('REFERENCE')),
      capture_mode: 'PAIR_SELECTED_EVIDENCE', origin,
      original_revision: document.source_sha256,
      extraction_revision: document.extractionRevision || '',
      coverage: location.coverage || 'complete', location: kind,
      url: '/reqpilotx/knowledge/tasks/' + encodeURIComponent(commandId) + '/documents/' + encodeURIComponent(document.id)
        + (binding.team?.kind === 'TOPIC' ? '?version=' + encodeURIComponent(binding.team.topic.version_id) : '') };
  }
  mapLiveHit(commandId, binding, document, hit) {
    return this.fact(commandId, binding, document, hit.text,
      { kind: hit.location?.kind || document.location, number: hit.location?.number,
        coverage: hit.coverage || document.coverage }, 'PAIR_SELECTED_SOURCE');
  }
  async search(commandId, query, limit = 8) {
    if (typeof query !== 'string' || !query.trim() || !Number.isInteger(limit) || limit < 1 || limit > 20)
      throw new KnowledgeError('INVALID_EVIDENCE_QUERY', 422);
    await this.requireCurrent(commandId, 'search');
    const binding = await this.loadBinding(commandId);
    if (binding.mode === 'ADOPTED_KNOWLEDGE') throw new KnowledgeError('ADOPTED_TOOLS_REQUIRED', 409,
      'Use the adopted knowledge evidence tools for this frozen library version.');
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
    if (binding.mode === 'ADOPTED_KNOWLEDGE') throw new KnowledgeError('ADOPTED_TOOLS_REQUIRED', 409,
      'Use the adopted knowledge read tool for this frozen library version.');
    const api = this.api();
    const documents = this.catalogDocuments(binding);
    let document = null, caller = null;
    if (binding.project?.documents.some(item => item.documentId === documentId)) {
      document = documents.find(item => item.id === documentId); caller = binding.project.caller;
      const read = await api.readSelectedSources(caller,
        { version: 1, caller, kbIds: [binding.project.library_id], documents: binding.project.documents },
        documentId, firstPage, count);
      const facts = read.pages.map(page => this.fact(commandId, binding, document, page.text,
        { kind: document.location, number: page.number, coverage: read.document.coverage }, 'PAIR_SELECTED_SOURCE'));
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
        { kind: document.location, number: page.number, coverage: read.document.coverage }, 'PAIR_SELECTED_SOURCE'));
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
    if (binding.mode === 'ADOPTED_KNOWLEDGE') throw new KnowledgeError('ADOPTED_TOOLS_REQUIRED', 409,
      'This task uses an adopted knowledge version, not a retained topic.');
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
    const documents = this.catalogDocuments(binding, consumed.facts);
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
  analysisHistoryPath(commandId) {
    return join(this.dataRoot, 'knowledge-analysis', commandId + '.history.json');
  }
  async loadAnalysisHistory(commandId, binding, taskId) {
    try {
      const value = await this.readJson(this.analysisHistoryPath(commandId));
      if (value?.schema_version !== 'reqpilot-pair-analysis-history/v1'
        || value.binding_sha256 !== binding.binding_sha256 || value.task_id !== taskId
        || !Array.isArray(value.entries)) throw new KnowledgeError('ANALYSIS_HISTORY_INVALID');
      return value;
    } catch (error) {
      if (error.code === 'ENOENT') return { schema_version: 'reqpilot-pair-analysis-history/v1',
        binding_sha256: binding.binding_sha256, task_id: taskId, entries: [] };
      throw error;
    }
  }
  async saveAnalysisHistory(commandId, history) {
    await this.writeJson(this.analysisHistoryPath(commandId), history);
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
    const batchIds = [...(unitIds || normalized.map(item => item.unit_id))].sort();
    if (normalized.some(item => !batchIds.includes(item.unit_id)))
      throw new KnowledgeError('INVALID_ANALYSIS', 422);
    const history = await this.loadAnalysisHistory(commandId, binding, record.prepared.task_id);
    const prior = history.entries.find(entry => entry.command_id === modelCommandId);
    if (prior && digest(prior.decisions) !== digest(normalized))
      throw new KnowledgeError('COMMAND_CONFLICT', 409, 'The analysis command already has different decisions');
    for (const decision of normalized) for (const citation of decision.evidence) {
      const fact = consumed.facts[citation.fact_id];
      if (!fact || !citation.quote.trim() || !fact.text.includes(citation.quote))
        throw new KnowledgeError('EVIDENCE_FACT_NOT_CONSUMED', 409, 'Cite only exact quotes from logged task evidence results');
    }
    const { catalog, registration } = await this.writeCatalog(commandId);
    const cumulative = new Map();
    for (const entry of history.entries) for (const decision of entry.decisions) cumulative.set(decision.unit_id, decision);
    for (const decision of normalized) cumulative.set(decision.unit_id, decision);
    const cumulativeDecisions = [...cumulative.values()].sort((left, right) => left.unit_id.localeCompare(right.unit_id));
    if (!this.runtime.attestationKey) throw new KnowledgeError('HOST_ATTESTATION_KEY_UNAVAILABLE', 503);
    const payload = { schema: 'reqpilot-pair-analysis-attestation/v1',
      application_id: binding.application_id, project_id: binding.project_id,
      document_id: record.documentId || '', task_id: record.prepared.task_id,
      workbench_ref: record.prepared.work_ref || '',
      binding_sha256: binding.binding_sha256,
      mode: binding.mode, task_revision: expectedRevision, command_id: modelCommandId,
      unit_ids: batchIds, decisions: cumulativeDecisions,
      decisions_sha256: digest(cumulativeDecisions), catalog_sha256: catalog.catalog_sha256,
      ledger_sha256: digest({ facts: consumed.facts, receipts: consumed.receipts }),
      consumed_fact_ids: Object.keys(consumed.facts).sort(),
      checked_at: new Date().toISOString() };
    const signature = createHmac('sha256', Buffer.from(this.runtime.attestationKey, 'hex'))
      .update(digest(payload)).digest('hex');
    const attestation = { ...payload, signature };
    const workspace = record.prepared.workspace;
    const directory = join(this.runtime.dataRoot, 'knowledge-analysis');
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const requestFile = join(directory,
      commandId + '-analysis-' + randomBytes(16).toString('hex') + '.json');
    const request = { task_id: record.prepared.task_id, expected_revision: expectedRevision,
      command_id: modelCommandId, decisions: normalized,
      unit_ids: batchIds, knowledge_attestation: attestation };
    await writeFile(requestFile, JSON.stringify(request), { mode: 0o600, flag: 'wx' });
    const result = await this.taskCommand(record, 'analysis', requestFile,
      { expected_revision: expectedRevision }, { attestation: true, timeout: 300000 });
    if (result.status === 'FAILED' || result.status === 'UNKNOWN') throw new KnowledgeError(
      result.error_code || 'ANALYSIS_NOT_SAVED', 409, this.redact(result.message || result.error || result.status));
    if (!prior) history.entries.push({ command_id: modelCommandId, task_revision: expectedRevision,
      decisions: normalized, checked_at: new Date().toISOString() });
    await this.saveAnalysisHistory(commandId, history);
    return result;
  }
  adoptedBindingFor(binding) {
    if (binding?.mode !== 'ADOPTED_KNOWLEDGE' || !binding.adopted
      || typeof binding.adopted.library_id !== 'string' || typeof binding.adopted.version_id !== 'string'
      || typeof binding.adopted.caller !== 'string')
      throw new KnowledgeError('ADOPTED_NOT_SELECTED', 409, 'This task has no frozen adopted knowledge library.');
    return binding.adopted;
  }
  async requireAdoptedCurrent(commandId) {
    const binding = await this.loadBinding(commandId);
    const frozen = this.adoptedBindingFor(binding);
    let api;
    try { api = this.api(); }
    catch (error) { throw adoptionFailure(error); }
    let current;
    try { current = api.adoptedBinding(frozen.caller); }
    catch (error) { throw adoptionFailure(error); }
    if (!current || current.sessionId !== frozen.caller || current.libraryId !== frozen.library_id
      || current.versionId !== frozen.version_id)
      throw new KnowledgeError('ADOPTED_BINDING_CHANGED', 409,
        'The task caller is no longer bound to the exact frozen adopted knowledge version.');
    let summary;
    try { summary = (api.adoptedLibraries?.() || []).map(adoptedSummary).filter(Boolean).find(item => item.id === frozen.library_id); }
    catch (error) { throw adoptionFailure(error); }
    if (!summary) throw new KnowledgeError('ADOPTED_LIBRARY_UNAVAILABLE', 409,
      'The frozen adopted knowledge library is not registered on this Host.');
    if (summary.versionId !== frozen.version_id) throw new KnowledgeError('ADOPTED_VERSION_CHANGED', 409,
      'The adopted knowledge library changed; the frozen version was not replaced with a newer one.');
    if (summary.state === 'blocked') throw new KnowledgeError('ADOPTED_LIBRARY_BLOCKED', 409,
      'The adopted knowledge library has blocking inspection problems.');
    return binding;
  }
  assertAdoptedResult(binding, result) {
    const frozen = this.adoptedBindingFor(binding);
    if (String(result?.libraryId || '') !== frozen.library_id || String(result?.versionId || '') !== frozen.version_id)
      throw new KnowledgeError('ADOPTED_VERSION_CHANGED', 409,
        'The adopted knowledge read returned a different library or version; the result was discarded.');
  }
  adoptedFact(commandId, binding, { item = null, result = null, text = '' }) {
    const frozen = this.adoptedBindingFor(binding);
    const identity = adoptedIdentity(item, result);
    if (!identity) return null;
    const kind = identity.kind, recordId = identity.id;
    const contentRevision = result?.contentRevision || item?.contentRevision;
    if (!contentRevision) return null;
    const sourceId = result?.sourceId || item?.sourceId;
    const sourceVersion = result?.sourceVersion || item?.sourceVersion;
    const originalSourceVersion = result?.originalSourceVersion || item?.originalSourceVersion;
    const availability = result?.availability || item?.availability || 'not-applicable';
    const admission = result?.admission || item?.admission;
    const locator = result?.locator || item?.locator;
    const review = result?.review || item?.review;
    const scope = result?.scope ?? item?.scope;
    const conditions = result?.conditions ?? item?.conditions;
    const guardrail = item?.guardrail;
    const origin = item?.origin === 'experience' || item?.origin === 'conflict' || item?.origin === 'gap'
      ? item.origin : kind === 'experience' || kind === 'conflict' || kind === 'gap'
        ? kind : 'derived';
    const body = String(text || '').slice(0, 100000);
    if (!body.trim()) return null;
    const page = locator?.kind === 'physical-page' && Number.isSafeInteger(locator.page) && locator.page > 0 ? locator.page : null;
    const section = locator?.kind === 'logical-section'
      ? (typeof locator.section === 'string' && locator.section ? locator.section
        : typeof locator.text === 'string' && locator.text ? locator.text : null) : null;
    const primaryId = kind + ':' + recordId;
    const linkedSource = Boolean(sourceId) && !['metadata-only', 'missing'].includes(availability);
    return {
      fact_id: 'pair-adopted-fact-' + digest({ binding: binding.binding_sha256, primaryId,
        contentRevision, text: body }).slice(0, 32),
      document_id: primaryId, kb_id: frozen.library_id, project_id: binding.project_id,
      source_sha256: contentRevision, source_sha256_basis: 'CONTENT_REVISION',
      role: 'INTERNAL_KNOWLEDGE', text: body, text_sha256: sha(body),
      returned_content_sha256: sha(body),
      source_ref: { ...adoptedLocator(locator), document_id: primaryId, coverage: 'unknown' },
      document_name: result?.title || item?.title || primaryId,
      document_version: String(contentRevision).slice(0, 12),
      authority: 'UNKNOWN', claim_role: 'REFERENCE_CONTEXT', applicability: 'UNKNOWN',
      eligible: true, authority_verified: false,
      capture_mode: 'PAIR_ADOPTED_LIBRARY', origin: 'PAIR_ADOPTED_KNOWLEDGE',
      evidence_origin: origin, content_revision: contentRevision,
      original_read_proof: 'NOT_READ',
      ...(sourceVersion ? { source_version: sourceVersion } : {}),
      ...(originalSourceVersion ? { original_source_version: originalSourceVersion } : {}),
      ...(availability ? { availability } : {}),
      ...(admission ? { admission } : {}),
      ...(locator ? { locator } : {}),
      ...(review ? { review, expert_approved_declared: review.expertApproved === true,
        expert_approved_type: typeof review.expertApproved } : {}),
      ...(scope === undefined ? {} : { scope }),
      ...(conditions === undefined ? {} : { conditions }),
      ...(guardrail === undefined ? {} : { guardrail }),
      url: adoptedRecordUrl(commandId, kind, recordId),
      ...(linkedSource ? { linked_source_id: sourceId, linked_source_url: adoptedSourceUrl(commandId, sourceId),
        linked_source_availability: availability } : {}),
      adopted: { library_id: frozen.library_id, version_id: frozen.version_id,
        package_version: frozen.package_version ?? null, evidence_id: item?.evidenceId || primaryId,
        kind, record_id: recordId, evidence_origin: origin, content_revision: contentRevision,
        ...(sourceId ? { linked_source_id: sourceId } : {}),
        ...(sourceVersion ? { source_version: sourceVersion } : {}),
        ...(originalSourceVersion ? { original_source_version: originalSourceVersion } : {}),
        ...(availability ? { availability } : {}),
        ...(admission ? { admission } : {}) },
    };
  }
  async adoptedStatus(commandId) {
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    const summary = (this.service.adoptedLibraries?.() || []).map(adoptedSummary).filter(Boolean)
      .find(item => item.id === frozen.library_id);
    return { bindingSha256: binding.binding_sha256, libraryId: frozen.library_id, versionId: frozen.version_id,
      caller: frozen.caller, name: frozen.name, packageVersion: frozen.package_version ?? null,
      state: summary?.state || 'unavailable', recommendation: summary?.recommendation || null,
      counts: summary?.counts || null, checks: summary?.checks || [] };
  }
  async adoptedBrowse(commandId, section, options = {}) {
    if (!ADOPTED_SECTIONS.includes(section)) throw new KnowledgeError('INVALID_ADOPTED_SECTION', 422);
    const query = options?.query;
    const limit = options?.limit;
    if (query !== undefined && typeof query !== 'string') throw new KnowledgeError('INVALID_ADOPTED_QUERY', 422);
    if (limit !== undefined && (!Number.isInteger(limit) || limit < 1 || limit > 100))
      throw new KnowledgeError('INVALID_ADOPTED_QUERY', 422);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    let result;
    try {
      result = await this.service.browseAdopted(frozen.caller, section,
        { ...(query === undefined ? {} : { query }), ...(limit === undefined ? {} : { limit }) });
    } catch (error) { throw adoptionFailure(error); }
    this.assertAdoptedResult(binding, result);
    await this.requireAdoptedCurrent(commandId);
    await this.recordConsumption(commandId, 'adopted_browse', { section, query: query ?? '', limit: limit ?? null }, []);
    return { bindingSha256: binding.binding_sha256, libraryId: result.libraryId, versionId: result.versionId,
      section: result.section, items: result.items, total: result.total, truncated: result.truncated };
  }
  async adoptedEvidence(commandId, query, limit = 8) {
    if (typeof query !== 'string' || !query.trim() || !Number.isInteger(limit) || limit < 1 || limit > 20)
      throw new KnowledgeError('INVALID_EVIDENCE_QUERY', 422);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    let result;
    try { result = await this.service.adoptedEvidence(frozen.caller, query, limit); }
    catch (error) { throw adoptionFailure(error); }
    this.assertAdoptedResult(binding, result);
    await this.requireAdoptedCurrent(commandId);
    const facts = [];
    const warnings = [...(Array.isArray(result.warnings) ? result.warnings : [])];
    for (const item of result.items || []) {
      const fact = this.adoptedFact(commandId, binding, { item, text: item.excerpt });
      if (fact) facts.push(fact);
      else warnings.push('content-revision-unavailable:' + String(item.evidenceId || 'unknown'));
    }
    const insufficiencies = [...(Array.isArray(result.insufficiencies) ? result.insufficiencies : [])];
    if (!facts.length && !insufficiencies.includes('no-matching-evidence')) insufficiencies.push('no-matching-evidence');
    await this.recordConsumption(commandId, 'adopted_evidence', { query, limit }, facts);
    return { bindingSha256: binding.binding_sha256, libraryId: result.libraryId, versionId: result.versionId,
      packageVersion: result.packageVersion ?? null, query: result.query, facts,
      coverage: result.coverage, warnings, insufficiencies };
  }
  async adoptedReadRun(commandId, value, consume) {
    const request = adoptedRequest(value);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    let result;
    try { result = await this.service.readAdopted(frozen.caller, request); }
    catch (error) { throw adoptionFailure(error); }
    this.assertAdoptedResult(binding, result);
    let raw = typeof result.content === 'string' ? result.content : '';
    if (request.kind === 'source' && !raw.trim()) {
      if (result.availability === 'metadata-only') throw new KnowledgeError('ADOPTED_METADATA_ONLY', 409,
        'Only metadata was admitted for this source; no normalized or original body was consumed.');
      throw new KnowledgeError('ADOPTED_SOURCE_CONTENT_UNAVAILABLE', 409,
        'The selected source has no readable normalized content; call knowledge_adopted_source with sourceId '
        + JSON.stringify(request.sourceId) + ' to retrieve the exact original or approved-derived bytes through the task-bound media route.');
    }
    if (!raw && result.record !== undefined) raw = JSON.stringify(result.record, null, 2);
    if (!raw.trim()) throw new KnowledgeError('ADOPTED_CONTENT_UNAVAILABLE', 409,
      'The selected record returned no readable content.');
    if (!result.contentRevision) throw new KnowledgeError('ADOPTED_CONTENT_UNVERIFIED', 409,
      'The selected version did not assert a contentRevision for the consumed record.');
    const truncated = raw.length > 100000;
    const text = truncated ? raw.slice(0, 100000) : raw;
    const fact = this.adoptedFact(commandId, binding, { result, text });
    if (!fact) throw new KnowledgeError('ADOPTED_CONTENT_UNVERIFIED', 409,
      'No verified text content is available for this selected record.');
    await this.requireAdoptedCurrent(commandId);
    if (consume) await this.recordConsumption(commandId, 'adopted_read', { request }, [fact]);
    return { bindingSha256: binding.binding_sha256, result: { ...result, content: text },
      fact, returnedContentSha256: sha(Buffer.from(text)), contentTruncated: truncated };
  }
  async adoptedRead(commandId, request) { return this.adoptedReadRun(commandId, request, true); }
  async adoptedRecord(commandId, request) { return this.adoptedReadRun(commandId, request, false); }
  async adoptedSource(commandId, sourceId) {
    if (!ADOPTED_ID.test(sourceId || '') || sourceId.split('/').includes('..'))
      throw new KnowledgeError('INVALID_ADOPTED_SOURCE', 422);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    let value;
    try { value = await this.service.adoptedSource(frozen.library_id, frozen.version_id, sourceId, frozen.caller); }
    catch (error) { throw adoptionFailure(error); }
    if (!value || !Buffer.isBuffer(value.bytes)) throw new KnowledgeError('ADOPTED_SOURCE_UNAVAILABLE', 409);
    const actual = sha(value.bytes);
    if (!value.contentRevision || actual !== value.contentRevision)
      throw new KnowledgeError('ADOPTED_CONTENT_REVISION_MISMATCH', 409,
        'The source bytes do not match the selected version contentRevision.');
    await this.requireAdoptedCurrent(commandId);
    return { ...value, sha256: actual };
  }
  async adoptedImage(commandId, imageId) {
    if (!ADOPTED_ID.test(imageId || '') || imageId.split('/').includes('..'))
      throw new KnowledgeError('INVALID_ADOPTED_IMAGE', 422);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    let value;
    try { value = await this.service.adoptedImage(frozen.library_id, frozen.version_id, imageId, frozen.caller); }
    catch (error) { throw adoptionFailure(error); }
    if (!value || !Buffer.isBuffer(value.bytes)) throw new KnowledgeError('ADOPTED_SOURCE_UNAVAILABLE', 409);
    await this.requireAdoptedCurrent(commandId);
    return value;
  }
  mediaCacheRoot(commandId) {
    return join(this.runtime?.dataRoot || this.dataRoot, 'task-media', commandId);
  }
  mediaReceiptPath(commandId) {
    return join(this.runtime?.dataRoot || this.dataRoot, 'knowledge-media', commandId + '.receipts.json');
  }
  async loadMediaReceipts(commandId) {
    try {
      const value = await this.readJson(this.mediaReceiptPath(commandId));
      if (value?.schema_version !== MEDIA_RECEIPTS_SCHEMA || !Array.isArray(value.entries))
        throw new KnowledgeError('MEDIA_RECEIPTS_INVALID', 409);
      return value;
    } catch (error) {
      if (error.code === 'ENOENT') return { schema_version: MEDIA_RECEIPTS_SCHEMA, entries: [] };
      throw error;
    }
  }
  async recordMediaReceipt(commandId, tool, request, media) {
    const run = this.consumption(commandId).then(async () => {
      const binding = await this.loadBinding(commandId);
      const current = await this.loadMediaReceipts(commandId);
      if (current.binding_sha256 && current.binding_sha256 !== binding.binding_sha256)
        throw new KnowledgeError('KNOWLEDGE_BINDING_CONFLICT');
      current.schema_version = MEDIA_RECEIPTS_SCHEMA;
      current.binding_sha256 = binding.binding_sha256;
      current.task_id = this.getRecord(commandId)?.prepared?.task_id || null;
      current.entries.push({ tool, request_sha256: digest(request), returned_at: new Date().toISOString(),
        kind: media.kind, id: media.id, library_id: media.libraryId, version_id: media.versionId,
        metadata: media.metadata ?? 'AVAILABLE',
        availability: media.availability ?? null, content_revision: media.contentRevision ?? null,
        record_content_revision: media.recordContentRevision ?? null,
        bytes_sha256: media.bytesSha256, bytes: media.bytes, mime: media.mime,
        filename: media.filename, cache_file: join('task-media', commandId, basename(media.path)),
        authority: 'MEDIA_BYTES_NOT_EVIDENCE', human_acceptance: 'NOT_CLAIMED' });
      if (current.entries.length > MEDIA_RECEIPT_LIMIT) current.entries = current.entries.slice(-MEDIA_RECEIPT_LIMIT);
      await this.writeJson(this.mediaReceiptPath(commandId), current);
    });
    this.pending.set(commandId, run.catch(() => {}));
    return run;
  }
  async materializeMedia(commandId, kind, id, value) {
    if (!value || !Buffer.isBuffer(value.bytes))
      throw new KnowledgeError('ADOPTED_SOURCE_UNAVAILABLE', 409);
    if (value.bytes.length > MEDIA_MAX_BYTES)
      throw new KnowledgeError('ADOPTED_MEDIA_TOO_LARGE', 413,
        'The selected media exceeds the Host read limit; no partial file was cached.');
    const actual = sha(value.bytes);
    const fallback = kind + '-' + String(id).replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 80);
    const filename = safeMediaName(value.filename, fallback);
    const root = this.mediaCacheRoot(commandId);
    await mkdir(root, { recursive: true, mode: 0o700 });
    const file = join(root, actual + '-' + filename);
    const temporary = file + '.' + randomBytes(6).toString('hex') + '.tmp';
    await writeFile(temporary, value.bytes, { mode: 0o400 });
    await rename(temporary, file);
    await chmod(file, 0o400);
    return { path: file, filename, mime: mediaMime(value.mime), sha256: actual, bytes: value.bytes.length };
  }
  async adoptedSourceMedia(commandId, sourceId) {
    if (!ADOPTED_ID.test(sourceId || '') || sourceId.split('/').includes('..'))
      throw new KnowledgeError('INVALID_ADOPTED_SOURCE', 422);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    let record = null;
    try {
      record = await this.service.readAdopted(frozen.caller, { kind: 'source', sourceId });
      this.assertAdoptedResult(binding, record);
      if (record.kind !== 'source' || (record.sourceId || record.id) !== sourceId)
        throw new KnowledgeError('ADOPTED_CONTENT_UNVERIFIED', 409,
          'The adopted source read returned a different source identity.');
    } catch (error) {
      if (error instanceof KnowledgeError) throw error;
      record = null;
    }
    const value = await this.adoptedSource(commandId, sourceId);
    const media = await this.materializeMedia(commandId, 'source', sourceId, {
      bytes: value.bytes, mime: value.mime, filename: value.filename });
    const result = { schema: 'reqpilot-adopted-media/v1', kind: 'source', id: sourceId,
      libraryId: frozen.library_id, versionId: frozen.version_id,
      title: bounded(record?.title, 500) || sourceId,
      availability: value.availability, admission: record?.admission ?? null,
      contentRevision: value.contentRevision, contentRevisionBasis: 'BINARY_FILE',
      sourceVersion: record?.sourceVersion ?? null, originalSourceVersion: record?.originalSourceVersion ?? null,
      locator: adoptedLocator(record?.locator), mime: media.mime, filename: media.filename,
      path: media.path, bytesSha256: media.sha256, bytes: media.bytes,
      identity: { kind: 'source', id: sourceId, library_id: frozen.library_id, version_id: frozen.version_id },
      authority: 'MEDIA_BYTES_NOT_EVIDENCE', humanAcceptance: 'NOT_CLAIMED' };
    await this.recordMediaReceipt(commandId, 'adopted_source', { sourceId }, result);
    return result;
  }
  async adoptedImageMedia(commandId, imageId) {
    if (!ADOPTED_ID.test(imageId || '') || imageId.split('/').includes('..'))
      throw new KnowledgeError('INVALID_ADOPTED_IMAGE', 422);
    const binding = await this.requireAdoptedCurrent(commandId);
    const frozen = binding.adopted;
    const value = await this.adoptedImage(commandId, imageId);
    let record = null;
    let metadata = 'AVAILABLE';
    try {
      record = await this.service.readAdopted(frozen.caller, { kind: 'image', id: imageId });
      this.assertAdoptedResult(binding, record);
      if (record.kind !== 'image' || record.id !== imageId)
        throw new KnowledgeError('ADOPTED_CONTENT_UNVERIFIED', 409,
          'The adopted image read returned a different image identity.');
    } catch (error) {
      if (error instanceof KnowledgeError) throw error;
      const code = String(error?.code || '');
      // Only an explicit record absence may omit locator/revision metadata.
      // Version, binding, permission and transport failures must stay failures.
      if (code === 'source-unavailable' || code === 'not-found') {
        record = null; metadata = 'UNAVAILABLE';
      } else {
        throw adoptionFailure(error);
      }
    }
    await this.requireAdoptedCurrent(commandId);
    const mediaPath = record?.path
      || (typeof record?.record?.image_path === 'string' ? record.record.image_path : null);
    const media = await this.materializeMedia(commandId, 'image', imageId, {
      bytes: value.bytes, mime: value.mime, filename: mediaPath ? basename(mediaPath) : null });
    await this.requireAdoptedCurrent(commandId);
    const result = { schema: 'reqpilot-adopted-media/v1', kind: 'image', id: imageId,
      libraryId: frozen.library_id, versionId: frozen.version_id,
      title: bounded(record?.title, 500) || imageId, metadata,
      availability: record?.availability ?? null, admission: record?.admission ?? null,
      recordContentRevision: record?.contentRevision ?? null,
      recordContentRevisionBasis: 'RECORD_TABLE_NOT_BINARY_BYTES',
      locator: adoptedLocator(record?.locator), mime: media.mime, filename: media.filename,
      path: media.path, bytesSha256: media.sha256, bytes: media.bytes,
      identity: { kind: 'image', id: imageId, library_id: frozen.library_id, version_id: frozen.version_id },
      authority: 'MEDIA_BYTES_NOT_EVIDENCE', humanAcceptance: 'NOT_CLAIMED' };
    await this.recordMediaReceipt(commandId, 'adopted_image', { imageId }, result);
    return result;
  }
  async original(commandId, documentId, versionId = null) {
    const binding = await this.loadBinding(commandId);
    const project = binding.project?.documents?.find(item => item.documentId === documentId);
    const team = binding.team?.documents?.find(item => item.documentId === documentId);
    if (project || team) {
      const scope = project
        ? { caller: binding.project.caller, kbId: binding.project.library_id, document: project, team: false }
        : { caller: binding.team.caller, kbId: binding.team.library_id, document: team, team: true };
      const verify = async () => {
        let captured;
        try {
          captured = await this.api().captureSelectedSources(scope.caller,
            [{ kbId: scope.kbId, documentId }]);
        } catch (error) {
          throw new KnowledgeError('KNOWLEDGE_EVIDENCE_UNAVAILABLE', 409,
            String(error?.message || error));
        }
        const current = captured.documents?.find(item => item.documentId === documentId);
        if (!current
          || current.originalRevision !== scope.document.originalRevision
          || current.extractionRevision !== scope.document.extractionRevision
          || current.coverage !== scope.document.coverage
          || current.location !== scope.document.location
          || current.pages !== scope.document.pages) {
          throw new KnowledgeError('KNOWLEDGE_EVIDENCE_CHANGED', 409,
            'The selected original or extraction version changed after the task was frozen');
        }
      };
      await verify();
      let value;
      try { value = await this.api().original(documentId); }
      catch (error) {
        throw new KnowledgeError('KNOWLEDGE_EVIDENCE_UNAVAILABLE', 409,
          String(error?.message || error));
      }
      const bytes = Buffer.from(value?.bytes || value || []);
      const actual = value?.document || {};
      // Selected-source captures own extraction identity; the Wiki backend revision is independent.
      if (actual.sha256 !== scope.document.originalRevision
        || sha(bytes) !== scope.document.originalRevision) {
        throw new KnowledgeError('KNOWLEDGE_EVIDENCE_CHANGED', 409,
          'Original bytes changed after the task was frozen');
      }
      await verify();
      return { document: { ...actual, id: documentId, kbId: scope.kbId,
        name: scope.document.name, mime: scope.document.mime,
        sha256: scope.document.originalRevision }, bytes };
    }
    if (binding.team?.kind === 'TOPIC' && versionId === binding.team.topic.version_id) {
      try {
        return await this.api().topicOriginal(binding.team.library_id,
          binding.team.topic.version_id, documentId);
      } catch (error) {
        throw new KnowledgeError('KNOWLEDGE_EVIDENCE_UNAVAILABLE', 409,
          String(error?.message || error));
      }
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
      else if (req.url === '/evidence/adopted/status') result = await this.adoptedStatus(commandId);
      else if (req.url === '/evidence/adopted/browse') result = await this.adoptedBrowse(commandId, body.section,
        { ...(body.query === undefined ? {} : { query: body.query }), ...(body.limit === undefined ? {} : { limit: body.limit }) });
      else if (req.url === '/evidence/adopted/read') result = await this.adoptedRead(commandId, body);
      else if (req.url === '/evidence/adopted/source') result = await this.adoptedSourceMedia(commandId, body.sourceId);
      else if (req.url === '/evidence/adopted/image') result = await this.adoptedImageMedia(commandId, body.imageId);
      else if (req.url === '/evidence/adopted/evidence') result = await this.adoptedEvidence(commandId, body.query, body.limit ?? 8);
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
