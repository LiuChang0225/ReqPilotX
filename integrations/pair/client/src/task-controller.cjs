/** Durable task identities are shared by the complete workbench and Pair's existing task API. */
function acceptedTaskReceipt(value, applicationId, input) {
  return Boolean(value?.schema === 'reqpilot-pair-launch/v1' && value.status === 'ACCEPTED' && value.applicationId === applicationId
    && ['commandId', 'projectId', 'documentId', 'sourceSha256'].every(key => value[key] === input[key])
    && typeof value.sessionId === 'string' && value.sessionId && typeof value.workspaceId === 'string' && value.workspaceId
    && /^plugin-[a-f0-9]{32}$/.test(value.workRef || ''));
}
function createPairTaskController(config, openSession, transport = fetch) {
  const taskPrefix = '/reqpilotx';
  const scopeKey = input => 'reqpilotx:intent:' + config.applicationId + ':' + input.projectId + ':' + input.documentId + ':' + input.sourceSha256;
  const read = key => { const value = localStorage.getItem(key); return value ? JSON.parse(value) : null; };
  const write = (key, value) => localStorage.setItem(key, JSON.stringify(value));
  const validIntent = (prior, input) => Boolean(prior && ['projectId', 'documentId', 'sourceSha256'].every(key => prior[key] === input[key])
    && /^[a-f0-9-]{36}$/.test(prior.commandId || '') && typeof prior.goal === 'string' && prior.goal.trim());
  const readIntent = input => {
    let prior;
    try { prior = read(scopeKey(input)); }
    catch (cause) { throw Error('原任务请求无法读取，请保留现场并核对本地记录后重试。', { cause }); }
    if (prior && !validIntent(prior, input)) {
      throw Error('原任务请求与当前文档不一致，请先核对记录；暂不创建新任务。');
    }
    return prior;
  };
  async function request(path, input) {
    const response = await transport(taskPrefix + path, input ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(input) } : {});
    const value = await response.json();
    if (!response.ok) throw Error(value.error || '无法确认任务，请保留原请求并重试。');
    return value;
  }
  async function restoreTask(input) {
    if (config.workbench?.taskLookupAvailable !== true) return null;
    const prior = readIntent(input);
    if (!prior?.commandId || input.workRef) {
      const query = new URLSearchParams(Object.entries(input).filter(([, value]) => value !== undefined));
      const listed = await request('/tasks?' + query.toString());
      if (!Array.isArray(listed.tasks)) throw Error('当前插件未提供持久任务查找，请更新后恢复原任务。');
      if (listed.tasks.length > 1) throw Error('找到多个已保存任务，请先从工作选择器选择要继续的工作。');
      const value = listed.tasks[0];
      if (!value) return null;
      if (!validIntent(value.request, input) || (input.workRef && value.workRef !== input.workRef)) throw Error('恢复结果与当前文档或工作不匹配，请核对原任务。');
      write(scopeKey(input), value.request);
      if (value.status === 'ACCEPTED' && !acceptedTaskReceipt(value, config.applicationId, value.request)) throw Error('已保存任务的绑定不一致，请核对原任务。');
      if (!acceptedTaskReceipt(value, config.applicationId, value.request)) return null;
      return request('/tasks/' + value.commandId);
    }
    const response = await transport(taskPrefix + '/tasks/' + prior.commandId);
    if (response.status === 404) return null;
    const value = await response.json();
    if (!response.ok) throw Error('暂时无法恢复原任务，请重试读取。');
    if (value.status === 'ACCEPTED' && !acceptedTaskReceipt(value, config.applicationId, prior)) throw Error('已保存任务的绑定不一致，请核对原任务。');
    if (!acceptedTaskReceipt(value, config.applicationId, prior)) return null;
    return value;
  }
  return {
    restoreTask,
    taskDraft: input => { const prior = readIntent(input); return prior ? { goal: prior.goal, requireMapConfirmation: prior.requireMapConfirmation === true } : undefined; },
    openSession,
    async startTask(input) {
      if (!config.capabilities['agent.start']) throw Error('当前连接未提供 PAIR 任务启动能力。');
      const key = scopeKey(input);
      const existing = await restoreTask(input);
      const prior = readIntent(input);
      const sameRequest = prior && prior.goal === input.goal && (prior.requireMapConfirmation === true) === (input.requireMapConfirmation === true);
      if (existing && sameRequest) return existing;
      if (prior && !existing && !sameRequest) throw Error('已有尚未确认的原任务，请先用原目标和确认策略恢复，再创建新任务。');
      const intent = prior && !existing ? prior : { ...input, commandId: crypto.randomUUID(), requireMapConfirmation: input.requireMapConfirmation === true };
      // Match the Host's request limits before retaining a command that cannot be sent.
      if (intent.goal.length > 12000 || new TextEncoder().encode(JSON.stringify(intent)).byteLength > 32768) throw Error('任务目标过长，请精简目标，资料通过文件导入后重试。');
      write(key, intent);
      const receipt = await request('/tasks/start', intent);
      if (!acceptedTaskReceipt(receipt, config.applicationId, intent)) throw Error('任务受理结果与原请求不匹配，请重试读取原任务。');
      return receipt;
    },
    async controlTask(receipt, action) {
      const key = 'reqpilotx:control:' + config.applicationId + ':' + receipt.commandId + ':' + action;
      const input = read(key) || { commandId: receipt.commandId, controlId: crypto.randomUUID(), action,
        sessionId: receipt.sessionId, workRef: receipt.workRef, sourceSha256: receipt.sourceSha256 };
      write(key, input);
      const result = await request('/tasks/' + receipt.commandId + '/control', input);
      if (result.control?.status !== 'ACCEPTED') throw Error('控制结果未确认，请重试同一请求。');
      localStorage.removeItem(key);
      return result;
    },
  };
}
module.exports.createPairTaskController = createPairTaskController;
