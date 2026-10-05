// Native project/document entry. All business data stays in the shared ReqPilotX API.
Object.assign(copy.zh, { goal: '任务目标', goalHint: '例如：整理原文结构和需求条目，自查后交付需求矩阵', startTask: '开始任务', confirmMap: '先确认结构再继续', openTask: '打开任务会话', accepted: '任务已提交到 Pair，可在会话中查看执行过程。', preparing: '正在准备并绑定任务…', projects: '项目与文档', newProject: '新建项目', projectName: '项目名称', customer: '客户名称', create: '创建项目', cancel: '取消', choose: '选择项目', noProjects: '新建一个项目，开始整理客户文档。', upload: '导入客户文档（PDF）', noDocs: '项目中还没有客户文档。', documents: '客户文档', back: '返回文档', original: '原文', results: '整理成果', noResults: '原文已保存，尚无整理成果。', opening: '正在读取…', saved: '原文已保存', unavailableRuntime: 'ReqPilotX 暂不可用', incompatibleRuntime: '插件与服务接口不兼容，请检查配套安装。', work: '打开成果', ready: '服务已就绪' });
Object.assign(copy.en, { goal: 'Task goal', goalHint: 'For example: organize the source and requirements, self-check and deliver a matrix', startTask: 'Start task', confirmMap: 'Wait for structure confirmation', openTask: 'Open task conversation', accepted: 'Task submitted to Pair. Follow execution in its conversation.', preparing: 'Preparing and binding task…', projects: 'Projects and documents', newProject: 'New project', projectName: 'Project name', customer: 'Customer name', create: 'Create project', cancel: 'Cancel', choose: 'Select project', noProjects: 'Create a project to organize customer documents.', upload: 'Import customer document (PDF)', noDocs: 'No customer documents in this project yet.', documents: 'Customer documents', back: 'Back to documents', original: 'Source document', results: 'Saved work', noResults: 'Source document saved. No extraction result yet.', opening: 'Loading…', saved: 'Source saved', unavailableRuntime: 'ReqPilotX is unavailable', incompatibleRuntime: 'Plugin and service interfaces are incompatible. Check the installed release.', work: 'Open result', ready: 'Service ready' });
Object.assign(copy.zh, { activityMODEL: 'Agent 正在处理', activityTOOLS: '正在执行工具', activityRETRY: '请求失败，等待重试', activityTURN_ENDED: '会话本轮已结束，成果仍需核对', activityFAILED: '本轮执行失败，请查看会话', activityBLOCKED: '等待处理会话中的阻塞', activityINTERRUPTED: '执行已中断', activityIDLE: '会话当前空闲', activityUNOBSERVED: '暂时无法确认执行状态', progressUnavailable: '暂时无法读取执行状态', progressSteps: '模型步骤', progressTools: '工具调用', progressRetries: '重试', progressToolFailures: '工具错误' });
Object.assign(copy.en, { activityMODEL: 'Agent is processing', activityTOOLS: 'Executing tools', activityRETRY: 'Request failed; waiting to retry', activityTURN_ENDED: 'Turn ended; results still require review', activityFAILED: 'Turn failed; inspect the conversation', activityBLOCKED: 'Waiting for a blocked conversation', activityINTERRUPTED: 'Execution interrupted', activityIDLE: 'Conversation is idle', activityUNOBSERVED: 'Execution status is unconfirmed', progressUnavailable: 'Execution status is unavailable', progressSteps: 'Model step', progressTools: 'Tool calls', progressRetries: 'Retries', progressToolFailures: 'Tool errors' });
Object.assign(copy.zh, { reconnectTask: '核对并重新连接原任务', reconnectAccepted: '已核对并连接到原任务；可继续同一会话。', stopTurn: '停止本轮', continueTask: '继续同一任务', controlBusy: '正在提交控制请求…', stopAccepted: '已请求停止当前 Pair 会话轮次；请核对持久化成果。', continueAccepted: '已提交到原 Pair 会话；继续执行和成果仍需核对。' });
Object.assign(copy.en, { reconnectTask: 'Verify and reconnect this task', reconnectAccepted: 'Verified and reconnected the original task. You can continue this session.', stopTurn: 'Stop current turn', continueTask: 'Continue this task', controlBusy: 'Submitting control request…', stopAccepted: 'Asked Pair to stop this turn; review the saved result.', continueAccepted: 'Submitted to the original Pair session; verify continuation and saved work.' });
const pairWorkOperations = Object.freeze({ save_feedback: 'results.feedback', confirm_map: 'results.confirm_map',
  save_engineer_response: 'results.engineer_responses', save_delivery_version: 'results.save_version' });
function makeReadonlyHost(origin, onSelectionChange, applicationId, writes = false) {
  const identity = connectionIdentity(applicationId, origin);
  // Legacy true grants its original two operations; newer writes require explicit discovery.
  const supplied = writes === true ? { 'results.feedback': true, 'results.confirm_map': true } : writes || {};
  const discovered = Object.freeze(Object.fromEntries(Object.values(pairWorkOperations).map(operation => [operation, supplied[operation] === true])));
  const managed = /^reqpilotx-pair:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(applicationId || '');
  const workCapabilities = Object.freeze(Object.fromEntries(Object.entries(pairWorkOperations).map(([action, operation]) => [action, managed && discovered[operation] === true])));
  const feedbackWrites = Object.values(workCapabilities).some(value => value === true);
  const client = feedbackWrites ? { id: identity.clientId, request: (path, init, options) => {
    const method = (init?.method || 'GET').toUpperCase(), operation = resultWriteOperation(method, path);
    if (!['GET', 'HEAD'].includes(method) && !(operation && managed && discovered[operation] === true)) throw Error('Pair 当前不允许此工作面写入操作。');
    return requestWithTransport(fetch, assetUrl(path, origin), init, options);
  } } : createReadonlyHttpClient(identity.clientId, path => assetUrl(path, origin));
  return { id: identity.hostId, label: 'Pair', client, feedbackWrites, workCapabilities, resourceUrl: path => assetUrl(path, origin), onSelectionChange,
    pdfViewerUrl: (source, page, embedded) => {
      const parsed = new URL(source, window.location.origin);
      const original = parsed.pathname.startsWith(PREFIX + '/service/api/') ? new URL(parsed.pathname.slice((PREFIX + '/service').length) + parsed.search, origin) : new URL(source, origin);
      if (original.origin !== origin || !original.pathname.startsWith('/api/')) throw Error('Unsupported PDF source.');
      const viewer = new URL('/pdf-viewer', origin); viewer.searchParams.set('pdf', original.href); viewer.searchParams.set('page', String(page));
      if (embedded) { viewer.searchParams.set('embedded', '1'); viewer.searchParams.set('parentOrigin', window.location.origin); }
      return viewer.href;
    } };
}
// Export the browser transport policy for contract tests; Cordis only invokes apply.
module.exports.makePairWorkbenchHost = makeReadonlyHost;
function Application({ t, openSession }) {
  const [config, setConfig] = React.useState(null);
  React.useEffect(() => {
    let disposed = false; const abort = new AbortController(); let timer;
    async function poll() {
      try { const response = await fetch(PREFIX + '/config', { signal: abort.signal }); if (!response.ok) throw Error(); const next = assertCompatibleConfiguration(await response.json()); if (!disposed) setConfig(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next); }
      catch (error) { if (!disposed) setConfig({ status: 'ERROR', message: t(String(error.message || '').startsWith('HOST_') ? 'incompatibleRuntime' : 'unavailableRuntime') }); }
      if (!disposed) timer = setTimeout(poll, 2000);
    }
    void poll(); return () => { disposed = true; abort.abort(); clearTimeout(timer); };
  }, []);
  if (!config || config.status !== 'READY') return h('section', { className: 'reqpilotx-pair-home' }, h('h1', null, 'ReqPilotX'), h('p', { role: config?.status === 'ERROR' ? 'alert' : 'status' }, config?.message || t('loading')));
  return config.mode === 'managed' ? h(Catalog, { config, t, openSession, key: config.applicationId }) : h(Panel, { t });
}
function Catalog({ config, t, openSession }) {
  const viewKey = 'reqpilotx:view:' + config.applicationId;
  const [projects, setProjects] = React.useState([]), [projectId, setProjectId] = React.useState(() => { try { return JSON.parse(localStorage.getItem(viewKey))?.projectId || ''; } catch { return ''; } });
  const [documents, setDocuments] = React.useState([]), [document, setDocument] = React.useState(null);
  const [creating, setCreating] = React.useState(false), [name, setName] = React.useState(''), [customer, setCustomer] = React.useState('');
  const [busy, setBusy] = React.useState(false), [error, setError] = React.useState(''), [works, setWorks] = React.useState([]), [selection, setSelection] = React.useState(null);
  const [goal, setGoal] = React.useState(''), [confirmMap, setConfirmMap] = React.useState(false), [launch, setLaunch] = React.useState(null), [launching, setLaunching] = React.useState(false);
  const attempt = React.useRef(null);
  const [execution, setExecution] = React.useState(null), [executionUnavailable, setExecutionUnavailable] = React.useState(false);
  const [controlling, setControlling] = React.useState(''), [controlNote, setControlNote] = React.useState('');
  const origin = config.serviceOrigin;
  const request = React.useCallback((path, options) => requestWithTransport(fetch, assetUrl(path, origin), options), [origin]);
  const client = React.useMemo(() => createProjectDocumentClient(request), [request]);
  React.useEffect(() => { let live = true; client.projects().then(items => { if (live) setProjects(items); }).catch(e => { if (live) setError(e.message); }); return () => { live = false; }; }, [request]);
  React.useEffect(() => {
    setDocuments([]); setDocument(null); setSelection(null); if (!projectId) return;
    let live = true; client.sorDocuments(projectId).then(items => { if (live) { setDocuments(items); try { const prior = JSON.parse(localStorage.getItem(viewKey)); if (prior?.projectId === projectId) setDocument(items.find(d => d.id === prior.documentId) || null); } catch {} } }).catch(e => { if (live) setError(e.message); }); return () => { live = false; };
  }, [projectId, request]);
  React.useEffect(() => {
    if (!document) return;
    try { localStorage.setItem(viewKey, JSON.stringify({ projectId, documentId: document.id })); } catch {}
  }, [viewKey, projectId, document]);
  const intentKey = document ? 'reqpilotx:intent:' + config.applicationId + ':' + projectId + ':' + document.id + ':' + document.source_sha256 : null;
  React.useEffect(() => {
    attempt.current = null; setLaunch(null); setGoal(''); setConfirmMap(false);
    if (!intentKey) return; let live = true;
    try {
      const prior = JSON.parse(localStorage.getItem(intentKey));
      if (prior?.commandId && typeof prior.goal === 'string') {
        attempt.current = prior; setGoal(prior.goal); setConfirmMap(prior.requireMapConfirmation === true);
        fetch(PREFIX + '/tasks/' + prior.commandId).then(r => r.ok ? r.json() : null).then(value => { if (live && value) setLaunch(value); }).catch(() => { if (live) setError(t('unavailable')); });
      }
    } catch {}
    return () => { live = false; };
  }, [intentKey]);
  React.useEffect(() => {
    setWorks([]); setSelection(null); if (!document) return; let live = true;
    const refresh = () => request(projectDocumentPaths.works(projectId, document.id)).then(value => { if (live) setWorks(value.sessions); }).catch(e => { if (live) setError(e.message); });
    void refresh(); const timer = setInterval(refresh, 5000);
    return () => { live = false; clearInterval(timer); };
  }, [document, projectId, request]);
  const bound = launch?.applicationId === config.applicationId && launch.projectId === projectId
    && launch.documentId === document?.id && launch.sourceSha256 === document?.source_sha256 ? launch : null;
  React.useEffect(() => {
    setExecution(null); setExecutionUnavailable(false); if (!bound?.commandId) return;
    let live = true, timer; const abort = new AbortController();
    async function poll() {
      try {
        const response = await fetch(PREFIX + '/tasks/' + bound.commandId, { signal: abort.signal });
        if (!response.ok) throw Error();
        const value = await response.json();
        if (live) { setExecutionUnavailable(false);
          const matches = value.applicationId === config.applicationId && value.sessionId === bound.sessionId
          && value.projectId === projectId && value.documentId === document.id && value.sourceSha256 === document.source_sha256
          && value.workRef === bound.workRef;
          setExecution(matches ? value.execution || null : null);
          if (matches) setLaunch(previous => ({ ...previous, controlCapabilities: value.controlCapabilities })); }
      } catch { if (live) { setExecution(null); setExecutionUnavailable(true); } }
      if (live) timer = setTimeout(poll, 3000);
    }
    void poll(); return () => { live = false; abort.abort(); clearTimeout(timer); };
  }, [bound?.commandId, bound?.sessionId, bound?.workRef, config.applicationId, projectId, document]);
  const host = React.useMemo(() => {
    const result = makeReadonlyHost(origin, setSelection, config.applicationId,
      config.capabilities);
    if (bound?.workspaceId && bound.sessionId && bound.workRef) {
      result.sessionBinding = { hostId: config.applicationId, workspaceId: bound.workspaceId, sessionId: bound.sessionId,
        projectId, documentId: document.id, sourceSha256: document.source_sha256, workRef: bound.workRef };
      result.openSession = (binding, context) => openSession(binding.sessionId, context);
      const snapshot = execution ? { ...result.sessionBinding, ...execution, activityLabel: t('activity' + execution.activity) } : null;
      result.execution = { getSnapshot: () => snapshot, subscribe: () => () => {} };
    }
    return result;
  }, [origin, config.applicationId, config.capabilities, bound, projectId, document, openSession, execution, t]);
  async function startTask(event) {
    event.preventDefault(); if (!document || launching || !goal.trim() || !config.capabilities['agent.start']) return;
    setLaunching(true); setError('');
    const payload = attempt.current && attempt.current.projectId === projectId && attempt.current.documentId === document.id && attempt.current.sourceSha256 === document.source_sha256 && attempt.current.goal === goal && attempt.current.requireMapConfirmation === confirmMap ? attempt.current
      : { commandId: crypto.randomUUID(), projectId, documentId: document.id, sourceSha256: document.source_sha256, goal, requireMapConfirmation: confirmMap };
    attempt.current = payload; try { localStorage.setItem(intentKey, JSON.stringify(payload)); } catch {}
    try {
      const response = await fetch(PREFIX + '/tasks/start', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
      const value = await response.json(); if (!response.ok) throw Error(value.error || t('unavailable'));
      setLaunch(value);
    } catch (error) { setError(error.message || t('unavailable')); } finally { setLaunching(false); }
  }
  async function controlTask(action) {
    if (!bound || !execution?.connected || !execution.runId || controlling || !bound.controlCapabilities?.[action]
      || !config.capabilities[action === 'cancel' ? 'agent.cancel' : action === 'reconnect' ? 'agent.reconnect' : 'agent.resume']) return;
    const key = 'reqpilotx:control:' + config.applicationId + ':' + bound.commandId + ':' + action + ':' + execution.runId + (action === 'reconnect' ? ':' + config.instanceId : '');
    let controlId;
    try { controlId = localStorage.getItem(key); } catch {}
    if (!controlId) { controlId = crypto.randomUUID(); try { localStorage.setItem(key, controlId); } catch {} }
    setControlling(action); setControlNote(''); setError('');
    try {
      const response = await fetch(PREFIX + '/tasks/' + bound.commandId + '/control', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ commandId: bound.commandId, controlId, action, sessionId: bound.sessionId,
          workRef: bound.workRef, sourceSha256: bound.sourceSha256 }) });
      const result = await response.json(); if (!response.ok) throw Error(result.error || t('unavailable'));
      if (result.control?.controlId !== controlId || result.control?.status !== 'ACCEPTED'
        || result.sessionId !== bound.sessionId || result.workRef !== bound.workRef || result.sourceSha256 !== bound.sourceSha256)
        throw Error(t('unavailable'));
      if (action === 'reconnect') setLaunch(result);
      setControlNote(t(action === 'cancel' ? 'stopAccepted' : action === 'reconnect' ? 'reconnectAccepted' : 'continueAccepted'));
    } catch (cause) { setError(cause.message || t('unavailable')); } finally { setControlling(''); }
  }
  async function create(event) {
    event.preventDefault(); if (busy || !config.capabilities['projects.create']) return; setBusy(true); setError('');
    try { const project = await client.createProject(name.trim(), customer.trim()); setProjects(await client.projects()); setProjectId(project.id); setCreating(false); setName(''); setCustomer(''); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  async function upload(event) {
    const input = event.currentTarget, file = input.files?.[0]; if (!file || busy || !config.capabilities['documents.import']) return; setBusy(true); setError('');
    try { const created = await client.uploadSorDocument(projectId, file); setDocuments(await client.sorDocuments(projectId)); setDocument(created); }
    catch (e) { setError(e.message); } finally { setBusy(false); input.value = ''; }
  }
  const pdf = document ? host.pdfViewerUrl(new URL(projectDocumentPaths.source(projectId, document.id), origin).href, 1, true) : '';
  return h('section', { className: 'reqpilotx-pair-home' },
    h('header', { className: 'reqpilotx-pair-home-header' }, h('div', null, h('h1', null, 'ReqPilotX'), h('p', null, t('projects'))), h('span', { role: 'status' }, busy ? t('opening') : t('ready'))),
    error && h('p', { role: 'alert' }, error),
    h('div', { className: 'reqpilotx-pair-toolbar' }, h('label', null, t('choose'), h('select', { value: projectId, disabled: busy, onChange: e => setProjectId(e.target.value) }, h('option', { value: '' }, t('choose')), projects.map(p => h('option', { key: p.id, value: p.id }, p.name)))), h('button', { onClick: () => setCreating(!creating), disabled: busy || !config.capabilities['projects.create'] }, t('newProject'))),
    creating && h('form', { onSubmit: create, className: 'reqpilotx-pair-project-form' }, h('label', null, t('projectName'), h('input', { required: true, maxLength: 255, value: name, onChange: e => setName(e.target.value) })), h('label', null, t('customer'), h('input', { required: true, maxLength: 255, value: customer, onChange: e => setCustomer(e.target.value) })), h('button', { disabled: busy || !config.capabilities['projects.create'] || !name.trim() || !customer.trim() }, t('create'))),
    !projects.length && !creating && h('p', null, t('noProjects')),
    projectId && h('div', { className: 'reqpilotx-pair-document-area' }, h('label', { className: 'reqpilotx-pair-import' }, t('upload'), h('input', { type: 'file', accept: '.pdf,application/pdf', disabled: busy || !config.capabilities['documents.import'], onChange: upload, 'aria-label': t('upload') })),
      !document ? h(React.Fragment, null, !documents.length && h('p', null, t('noDocs')), h('ul', { className: 'reqpilotx-pair-documents' }, documents.map(d => h('li', { key: d.id }, h('button', { onClick: () => setDocument(d) }, d.file_name), h('span', null, t('saved'))))))
      : h(React.Fragment, null, h('div', { className: 'reqpilotx-pair-toolbar' }, h('button', { onClick: () => { setDocument(null); setSelection(null); } }, t('back')), h('strong', null, document.file_name)),
          config.capabilities['agent.start'] && h('form', { className: 'reqpilotx-pair-project-form reqpilotx-pair-task-form', onSubmit: startTask },
            h('label', null, t('goal'), h('textarea', { required: true, maxLength: 12000, value: goal, placeholder: t('goalHint'), disabled: launching, onChange: e => { setGoal(e.target.value); setLaunch(null); } })),
            h('label', null, h('input', { type: 'checkbox', checked: confirmMap, disabled: launching, onChange: e => setConfirmMap(e.target.checked) }), t('confirmMap')),
            h('button', { disabled: launching || !goal.trim() }, launching ? t('preparing') : t('startTask'))),
          bound?.status === 'ACCEPTED' && h('div', { className: 'reqpilotx-pair-toolbar' }, h('p', { role: 'status' }, execution ? t('activity' + execution.activity) : executionUnavailable ? t('progressUnavailable') : t('accepted')), execution && h('span', null, t('progressSteps') + ' ' + execution.step + ' · ' + t('progressTools') + ' ' + execution.toolCalls + ' · ' + t('progressRetries') + ' ' + execution.retries + ' · ' + t('progressToolFailures') + ' ' + execution.toolFailures), h('button', { onClick: () => openSession(bound.sessionId) }, t('openTask'))),
          bound?.status === 'ACCEPTED' && execution?.connected && h('div', { className: 'reqpilotx-pair-toolbar' },
            config.capabilities['agent.reconnect'] && bound.controlCapabilities?.reconnect && ['SUCCEEDED', 'FAILED', 'INTERRUPTED', 'IDLE'].includes(execution.status) && h('button', { type: 'button', disabled: Boolean(controlling), onClick: () => void controlTask('reconnect') }, controlling ? t('controlBusy') : t('reconnectTask')),
            config.capabilities['agent.cancel'] && bound.controlCapabilities?.cancel && execution.status === 'RUNNING' && h('button', { type: 'button', disabled: Boolean(controlling), onClick: () => void controlTask('cancel') }, controlling ? t('controlBusy') : t('stopTurn')),
            config.capabilities['agent.resume'] && bound.controlCapabilities?.continue && ['SUCCEEDED', 'FAILED', 'INTERRUPTED', 'IDLE'].includes(execution.status) && h('button', { type: 'button', disabled: Boolean(controlling), onClick: () => void controlTask('continue') }, controlling ? t('controlBusy') : t('continueTask')),
            controlNote && h('p', { role: 'status' }, controlNote)),
          selection ? h(ReqPilotXReadonlyPanel, { selection, documentName: document.file_name, host, active: true })
          : h('div', { className: 'reqpilotx-pair-document' }, h('section', null, h('h2', null, t('results')), !works.length && h('p', null, t('noResults')), works.map(work => h('button', { key: work.session_id, disabled: !work.revision_id, onClick: () => setSelection({ projectId, documentId: document.id, sourceSha256: document.source_sha256, workRef: work.session_id, revisionId: work.revision_id, view: 'structure' }) }, t('work') + ' · v' + work.revision_number))), h('iframe', { title: t('original'), src: pdf })))));
}
