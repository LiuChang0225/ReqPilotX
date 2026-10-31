// Native project/document entry. All business data stays in the shared ReqPilotX API.
Object.assign(copy.zh, { goal: '补充说明（可选）', goalHint: '例如：本次重点关注试验条件或某类条款；不会取消固定阶段与输出检查', startTask: '开始固定流程', confirmMap: '先核对结构，再继续整理条目', openTask: '打开任务会话', accepted: '已提交到 Pair，可在会话中查看执行；阶段完成仍以已保存成果为准。', preparing: '正在准备并绑定任务…', projects: '项目与文档', newProject: '新建项目', projectName: '项目名称', customer: '客户名称', create: '创建项目', cancel: '取消', choose: '选择项目', noProjects: '新建一个项目，开始整理客户文档。', upload: '导入客户文档（PDF）', noDocs: '项目中还没有客户文档。', documents: '客户文档', back: '返回文档', original: '原文', results: '整理成果', noResults: '原文已保存，尚无整理成果。', opening: '正在读取…', saved: '原文已保存', unavailableRuntime: 'ReqPilotX 暂不可用', incompatibleRuntime: '插件与服务接口不兼容，请检查配套安装。', work: '打开成果', ready: '服务已就绪' });
Object.assign(copy.en, { goal: 'Supplementary instructions (optional)', goalHint: 'For example: focus on test conditions or a clause type; this cannot remove fixed stages or output checks', startTask: 'Start fixed workflow', confirmMap: 'Check the structure before items', openTask: 'Open task conversation', accepted: 'Submitted to Pair. Follow execution in its conversation; saved artifacts still decide stage completion.', preparing: 'Preparing and binding task…', projects: 'Projects and documents', newProject: 'New project', projectName: 'Project name', customer: 'Customer name', create: 'Create project', cancel: 'Cancel', choose: 'Select project', noProjects: 'Create a project to organize customer documents.', upload: 'Import customer document (PDF)', noDocs: 'No customer documents in this project yet.', documents: 'Customer documents', back: 'Back to documents', original: 'Source document', results: 'Saved work', noResults: 'Source document saved. No extraction result yet.', opening: 'Loading…', saved: 'Source saved', unavailableRuntime: 'ReqPilotX is unavailable', incompatibleRuntime: 'Plugin and service interfaces are incompatible. Check the installed release.', work: 'Open result', ready: 'Service ready' });
Object.assign(copy.zh, { activityMODEL: 'Agent 正在处理', activityTOOLS: '正在执行工具', activityRETRY: '请求失败，等待重试', activityTURN_ENDED: '会话本轮已结束，成果仍需核对', activityFAILED: '本轮执行失败，请查看会话', activityBLOCKED: '等待处理会话中的阻塞', activityINTERRUPTED: '执行已中断', activityIDLE: '会话当前空闲', activityUNOBSERVED: '暂时无法确认执行状态', progressUnavailable: '暂时无法读取执行状态', progressSteps: '模型步骤', progressTools: '工具调用', progressRetries: '重试', progressToolFailures: '工具错误' });
Object.assign(copy.en, { activityMODEL: 'Agent is processing', activityTOOLS: 'Executing tools', activityRETRY: 'Request failed; waiting to retry', activityTURN_ENDED: 'Turn ended; results still require review', activityFAILED: 'Turn failed; inspect the conversation', activityBLOCKED: 'Waiting for a blocked conversation', activityINTERRUPTED: 'Execution interrupted', activityIDLE: 'Conversation is idle', activityUNOBSERVED: 'Execution status is unconfirmed', progressUnavailable: 'Execution status is unavailable', progressSteps: 'Model step', progressTools: 'Tool calls', progressRetries: 'Retries', progressToolFailures: 'Tool errors' });
Object.assign(copy.zh, { reconnectTask: '核对并重新连接原任务', reconnectAccepted: '已核对并连接到原任务；可继续同一会话。', stopTurn: '停止本轮', continueTask: '继续同一任务', controlBusy: '正在提交控制请求…', stopAccepted: '已请求停止当前 Pair 会话轮次；请核对持久化成果。', continueAccepted: '已提交到原 Pair 会话；继续执行和成果仍需核对。' });
Object.assign(copy.en, { reconnectTask: 'Verify and reconnect this task', reconnectAccepted: 'Verified and reconnected the original task. You can continue this session.', stopTurn: 'Stop current turn', continueTask: 'Continue this task', controlBusy: 'Submitting control request…', stopAccepted: 'Asked Pair to stop this turn; review the saved result.', continueAccepted: 'Submitted to the original Pair session; verify continuation and saved work.' });
const pairWorkOperations = Object.freeze({ save_feedback: 'results.feedback', confirm_map: 'results.confirm_map',
  save_engineer_response: 'results.engineer_responses', save_delivery_version: 'results.save_version' });
function knowledgeRequest(path, init) {
  return fetch(PREFIX + '/knowledge' + path, { cache: 'no-store', ...(init || {}) }).then(async response => {
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(value.message || value.error || '知识请求未完成');
    return value;
  });
}
function makeKnowledgeHost(config) {
  if (config?.knowledge?.schema !== 'reqpilot-knowledge-host/v1' || config.knowledge.available !== true) return null;
  return {
    available: true,
    inventory: projectId => knowledgeRequest('/inventory?' + new URLSearchParams({ projectId }).toString()),
    uploadReference: async (projectId, file, kind) => {
      const query = new URLSearchParams({ name: file.name, kind });
      return knowledgeRequest('/projects/' + encodeURIComponent(projectId) + '/references?' + query.toString(),
        { method: 'POST', headers: { 'content-type': file.type || 'application/octet-stream' }, body: file });
    },
    retryReference: (projectId, documentId) => knowledgeRequest('/projects/' + encodeURIComponent(projectId)
      + '/references/' + encodeURIComponent(documentId) + '/retry', { method: 'POST' }),
    teamTopics: async kbId => (await knowledgeRequest('/team/' + encodeURIComponent(kbId) + '/topics')).versions || [],
    teamLibraries: async () => (await knowledgeRequest('/team')).libraries || [],
  };
}
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
    const abort = new AbortController(); let timer;
    async function poll() {
      try {
        const response = await fetch(PREFIX + '/config', { signal: abort.signal });
        if (!response.ok) throw Error();
        const value = assertCompatibleConfiguration(await response.json());
        if (value.mode === 'managed' && value.status === 'READY' && value.workbench?.schema !== 'reqpilot-workbench-host/v1') throw Error('HOST_WORKBENCH_INCOMPATIBLE');
        if (!abort.signal.aborted) setConfig(previous => JSON.stringify(previous) === JSON.stringify(value) ? previous : value);
      } catch (error) { if (!abort.signal.aborted) setConfig({ status: 'ERROR', message: t(String(error.message).startsWith('HOST_') ? 'incompatibleRuntime' : 'unavailableRuntime') }); }
      if (!abort.signal.aborted) timer = setTimeout(poll, 2000);
    }
    void poll(); return () => { abort.abort(); clearTimeout(timer); };
  }, []);
  if (!config || config.status !== 'READY') return h('section', { className: 'reqpilotx-pair-open', role: config?.status === 'ERROR' ? 'alert' : 'status' },
    h('h1', null, 'ReqPilotX'), h('p', null, config?.message || t('loading')));
  if (config.mode !== 'managed') return h(Panel, { t });
  return h(HostedApplication, { key: config.applicationId + ':' + config.serviceOrigin, config, openSession });
}
function HostedApplication({ config, openSession }) {
  const [installed, setInstalled] = React.useState(false);
  const readTheme = () => window.getComputedStyle(document.documentElement).colorScheme.includes('dark') ? 'dark' : 'light';
  const [theme, setTheme] = React.useState(readTheme);
  React.useEffect(() => {
    const observer = new window.MutationObserver(() => setTheme(readTheme()));
    observer.observe(document.documentElement, { attributes: true });
    return () => observer.disconnect();
  }, []);
  const resources = React.useMemo(() => makeReadonlyHost(config.serviceOrigin, undefined, config.applicationId), [config.applicationId, config.serviceOrigin]);
  const host = React.useMemo(() => ({
    ...makeReadonlyHost(config.serviceOrigin, undefined, config.applicationId, config.capabilities),
    resourceUrl: resources.resourceUrl, pdfViewerUrl: resources.pdfViewerUrl,
    id: config.applicationId, label: 'PAIR', modelLabel: config.taskModel ? [config.taskModel.model, config.taskModel.reasoningEffort].filter(Boolean).join(' / ') : '尚未读取到 PAIR 默认模型', captureAvailable: config.capabilities['documents.capture'] === true,
    taskStartAvailable: config.capabilities['agent.start'] === true && Boolean(config.workflow) && config.taskStart?.ready !== false,
    workflow: config.workflow || null, taskSetup: config.taskStart?.reason || (config.workflow ? null : 'FIXED_WORKFLOW_UNAVAILABLE'),
    knowledge: makeKnowledgeHost(config),
    ...createPairTaskController(config, openSession),
  }), [config, resources, openSession]);
  React.useLayoutEffect(() => {
    const dispose = panelModule.exports.installApplicationTransport({ id: config.applicationId, resourceUrl: resources.resourceUrl,
      fetch: (input, init) => fetch(resources.resourceUrl(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url), init) });
    setInstalled(true); return dispose;
  }, [config.applicationId, resources]);
  return installed ? h(panelModule.exports.ReqPilotXApplication, { host, theme }) : h('p', { role: 'status' }, '正在打开 ReqPilotX 工作台…');
}
module.exports = { ...(module.exports || {}), makePairWorkbenchHost: makeReadonlyHost };
