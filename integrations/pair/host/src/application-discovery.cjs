/** Host transport contract only: business schemas and task/acceptance states stay in ReqPilotX. */
const APPLICATION_SCHEMA = 'reqpilot-application-host/v1';
const OPERATION_IDS = ['projects.read', 'projects.create', 'documents.read', 'documents.import', 'results.read', 'results.feedback', 'results.confirm_map', 'results.engineer_responses', 'results.save_version', 'agent.start', 'agent.resume', 'agent.cancel', 'events.agent'];
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
function applicationId(ownerId) { return typeof ownerId === 'string' && UUID.test(ownerId) ? 'reqpilotx-pair:' + ownerId : null; }
function capabilities(state) {
  const ready = state.status === 'READY';
  return {
    'projects.read': ready,
    'projects.create': ready && state.mode === 'managed',
    'documents.read': ready,
    'documents.import': ready && state.mode === 'managed',
    'results.read': ready,
    'results.feedback': ready && state.mode === 'managed',
    'results.confirm_map': ready && state.mode === 'managed',
    'results.engineer_responses': ready && state.mode === 'managed',
    'results.save_version': ready && state.mode === 'managed',
    'agent.start': ready && state.mode === 'managed' && state.taskLaunchReady === true,
    'agent.resume': ready && state.mode === 'managed' && state.taskContinueReady === true,
    'agent.cancel': ready && state.mode === 'managed' && state.taskCancelReady === true, 'events.agent': false,
  };
}
/** One exact-path policy is embedded into the Client and enforced again by the Host. */
const RESULT_WRITE_OPERATIONS = Object.freeze({ feedback: 'results.feedback', 'confirm-map': 'results.confirm_map',
  'engineer-responses': 'results.engineer_responses', versions: 'results.save_version' });
function resultWriteOperation(method, pathname) {
  if (method !== 'POST' || typeof pathname !== 'string') return null;
  const match = /^\/api\/projects\/[^/]+\/sor-documents\/[^/]+\/agent-structure\/delivery\/plugin-workbench\/plugin-[a-f0-9]{32}\/(feedback|confirm-map|engineer-responses|versions)$/.exec(pathname);
  return match ? RESULT_WRITE_OPERATIONS[match[1]] : null;
}
/** Transport write policy and capability discovery share the same implemented operations. */
function allowsWrite(state, method, pathname) {
  if (method !== 'POST') return false;
  const available = capabilities({ ...state, status: 'READY' });
  return (pathname === '/reqpilotx/service/api/projects' && available['projects.create'])
    || (/^\/reqpilotx\/service\/api\/projects\/[^/]+\/sor-documents$/.test(pathname) && available['documents.import'])
    || (pathname.startsWith('/reqpilotx/service/') && available[resultWriteOperation(method, pathname.slice('/reqpilotx/service'.length))] === true);
}
/** Whitelist public fields; never serialize a process snapshot or an installed-package descriptor wholesale. */
function publicConfiguration(state) {
  const result = {
    schema: APPLICATION_SCHEMA,
    mode: state.mode,
    status: state.status,
    applicationId: state.mode === 'managed' ? applicationId(state.ownerId) : null,
    instanceId: typeof state.instanceId === 'string' ? state.instanceId : null,
    stage: typeof state.stage === 'string' ? state.stage : null,
    message: state.status === 'ERROR' ? 'ReqPilotX 服务不可用，请检查宿主运行配置。' : String(state.message || ''),
    capabilities: capabilities(state),
  };
  if (state.status === 'READY') result.serviceOrigin = state.serviceOrigin;
  return result;
}
/** Fail on protocol/capability incompatibility before presenting business controls. */
function assertCompatibleConfiguration(value) {
  if (!value || value.schema !== APPLICATION_SCHEMA || !['managed', 'readonly-connect'].includes(value.mode)
    || !['STARTING', 'READY', 'ERROR', 'STOPPED'].includes(value.status)
    || OPERATION_IDS.some(id => typeof value.capabilities?.[id] !== 'boolean')) throw Error('HOST_PROTOCOL_INCOMPATIBLE');
  if (value.status === 'READY') {
    let url; try { url = new URL(value.serviceOrigin); } catch { throw Error('HOST_ENDPOINT_INVALID'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.origin !== value.serviceOrigin) throw Error('HOST_ENDPOINT_INVALID');
    if (value.mode === 'managed' && (typeof value.applicationId !== 'string'
      || !value.applicationId.startsWith('reqpilotx-pair:') || !UUID.test(value.applicationId.slice('reqpilotx-pair:'.length)))) throw Error('HOST_IDENTITY_MISSING');
  }
  return value;
}
/** Stable application identity is separate from the transport generation used to invalidate stale requests. */
function connectionIdentity(id, origin) {
  const hostId = id || 'pair-connect:' + origin;
  return { hostId, clientId: hostId + '@' + origin };
}
module.exports = { APPLICATION_SCHEMA, OPERATION_IDS, capabilities, allowsWrite, publicConfiguration, assertCompatibleConfiguration, connectionIdentity, resultWriteOperation };
