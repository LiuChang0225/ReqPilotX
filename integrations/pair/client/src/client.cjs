// Wrapped at build time with the shared panel CJS module and host-provided require.
const React = require('react');
const { ReqPilotXReadonlyPanel, createReadonlyHttpClient, validWorkSelection, createProjectDocumentClient, projectDocumentPaths, requestWithTransport } = panelModule.exports;
const h = React.createElement;
const PREFIX = '/reqpilotx';
const STORAGE = 'reqpilotx:readonly-selection:v1:';
const NS = 'reqpilotx-panel';
const copy = {
  zh: { 'workbench.locale': 'zh', 'panel.invalidSelection': '工作链接不完整，请提供准确的来源、工作及成果版本。', 'panel.reviewAria': 'ReqPilotX 核对与反馈', 'panel.readonlyAria': 'ReqPilotX 只读工作面板', title: 'ReqPilotX', intro: '打开已保存的需求成果', hint: '粘贴包含具体工作与版本的 ReqPilotX 工作台链接。', link: '工作台链接', open: '打开成果', change: '切换工作', loading: '正在连接 ReqPilotX…', invalid: '请提供当前服务中包含项目、文档和具体工作的工作台链接。', unavailable: '无法读取该工作，请检查链接和服务连接。' },
  en: { 'workbench.locale': 'en', 'panel.invalidSelection': 'The work link is incomplete. Provide the exact source, work and result version.', 'panel.reviewAria': 'ReqPilotX review and feedback', 'panel.readonlyAria': 'ReqPilotX read-only work panel', title: 'ReqPilotX', intro: 'Open saved requirement work', hint: 'Paste a ReqPilotX workbench link with its work and revision.', link: 'Workbench link', open: 'Open work', change: 'Change work', loading: 'Connecting to ReqPilotX…', invalid: 'Use a workbench link for this service with a project, document and explicit work.', unavailable: 'Could not read this work. Check the link and service connection.' },
};
Object.assign(copy.zh, {
  'workflow.noDescriptor': '当前宿主尚未提供固定流程描述；不会用聊天目标替代阶段检查。',
  'workflow.intakeAria': '固定业务流程', 'workflow.defaultLabel': '客户需求整理固定流程',
  'workflow.version': '版本 {version}', 'workflow.required': '必需资料', 'workflow.optional': '可选内部依据',
  'workflow.expected': '预期成果', 'workflow.customerSource': '客户需求文档', 'workflow.available': '可选择',
  'workflow.unavailable': '当前不可用', 'workflow.noOptional': '当前版本未接入项目参考或团队知识库。',
  'workflow.expectedFallback': '结构、原文条目、分类、自查与可核对成果。', 'workflow.statusAria': '固定流程成果',
  'workflow.statusHeading': '业务成果阶段', 'workflow.statusUnconfirmed': '状态未确认',
  'workflow.unconfirmed': '固定流程状态未确认：{reason}。已保存成果仍保留，请重新读取任务后再核对。',
  'workflow.noState': '固定流程状态尚未保存；任务尚未开始或成果尚未同步。',
  'workflow.unconfirmedReason': '派生状态与保存工件不一致', 'workflow.workflowVersion': '流程 v{version}',
  'workflow.ready': '已保存成果满足当前可核对条件；人工接受仍未声称。', 'workflow.next': '下一步：', 'workflow.currentStage': '当前：{stage}', 'workflow.viewDetails': '查看详情', 'workflow.hideDetails': '收起详情',
  'workflow.continueRemaining': '继续处理未决项', 'workflow.openItems': '未决项：',
  'workflow.stage.PENDING': '尚未保存', 'workflow.stage.IN_PROGRESS': '处理中', 'workflow.stage.SAVED': '已保存',
  'workflow.stage.SKIPPED': '按规则跳过', 'workflow.stage.WAITING': '等待输入', 'workflow.stage.BLOCKED': '等待前置阶段',
  'workflow.stageLabel.PREPARATION': '资料准备', 'workflow.stageLabel.SOURCE_MAP': '来源与结构',
  'workflow.stageLabel.ITEMS_CLASSIFICATION': '条目、分类与明确派生', 'workflow.stageLabel.EVIDENCE_ANALYSIS': '依据分析',
  'workflow.stageLabel.SELF_CHECK': '自查与修正', 'workflow.stageLabel.DELIVERABLE': '可核对成果',
  'workflow.reason.SOURCE_MAP_REQUIRED': '需要先保存文档结构', 'workflow.reason.ITEMS_NOT_SAVED': '尚未保存原文条目',
  'workflow.reason.ITEMS_REQUIRED': '需要先保存原文条目', 'workflow.reason.MAP_CONFIRMATION_REQUIRED': '等待确认这一版结构',
  'workflow.reason.SCOPED_DERIVATION_REMAINING': '明确授权的派生范围尚未处理完',
  'workflow.reason.UNPROCESSED_SOURCE_SCOPE': '仍有原文范围尚未处理', 'workflow.reason.NO_INTERNAL_EVIDENCE': '未提供内部依据',
  'workflow.reason.INTERNAL_EVIDENCE_NOT_WIRED': '当前版本尚未接入内部依据',
  'workflow.reason.INTERNAL_EVIDENCE_ANALYSIS_REQUIRED': '内部依据分析尚未保存',
  'workflow.reason.WORKFLOW_EVIDENCE_CONFLICT': '工作流声明与依据状态不一致',
  'workflow.reason.SELF_CHECK_NOT_SAVED': '尚无已保存自查', 'workflow.reason.SELF_CHECK_NOT_RUN': '尚未运行程序自查',
  'workflow.reason.SELF_CHECK_BASIS_CHANGED': '自查依据已变化，需要重新核对',
  'workflow.reason.SELF_CHECK_FAILED': '程序自查未通过', 'workflow.reason.DELIVERABLE_NOT_SAVED': '尚未保存交付成果',
  'workflow.reason.SELF_CHECK_REQUIRED': '需要先完成自查',
  'workflow.reason.INCOMPLETE_OPERATION': '存在结果未知或待恢复的程序操作',
  'workflow.reason.OPERATION_FAILURE': '保留失败尝试记录', 'workflow.reason.EVIDENCE_ANALYSIS_SKIPPED': '依据分析按声明跳过',
  'workflow.evidence.TASK_CONTEXT': '任务与来源', 'workflow.evidence.DOCUMENT_MAP': '文档结构',
  'workflow.evidence.SOURCE_ITEMS': '原文条目', 'workflow.evidence.DECOMPOSITION': '明确派生',
  'workflow.evidence.REVIEW_HEAD': '自查工作版', 'workflow.evidence.DELIVERY': '已保存交付',
  'workflow.evidence.WORKFLOW_BRANCH': '声明分支', 'workflow.evidence.WORKFLOW_CHECK': '程序自查',
  'workflow.evidence.DELIVERY_VERSION': '已保存交付版本', 'workflow.evidence.TASK_BINDING': '任务绑定',
  'workflow.optional.project_references.label': '项目参考资料',
  'workflow.optional.project_references.reason': '当前版本尚未接入项目参考资料。',
  'workflow.optional.team_knowledge.label': '团队知识库',
  'workflow.optional.team_knowledge.reason': '当前版本尚未接入团队知识库。',
  'workflow.expected.structure': '可回查原文与顺序的文档结构',
  'workflow.expected.items': '保留完整父项的原文条目、分类及明确授权的必要派生',
  'workflow.expected.evidence': '有内部依据时的引用分析；无内部依据时明确标记未执行',
  'workflow.expected.check': '程序自查结果与当前可核对成果版本',
  'execution.ACCEPTED': '已受理', 'execution.RUNNING': '正在处理', 'execution.WAITING': '等待中',
  'execution.SUCCEEDED': '本轮已结束', 'execution.FAILED': '本轮未完成', 'execution.INTERRUPTED': '已暂停',
  'execution.IDLE': '空闲', 'execution.UNKNOWN': '状态尚未确认',
  'hosted.taskNoun': 'PAIR 任务', 'hosted.sessionNoun': 'PAIR 会话',
  'control.openSession': '打开 PAIR 会话', 'control.stop': '停止本轮', 'control.continue': '继续同一任务',
  'control.reconnect': '核对并重连原任务',
  'intake.title': '按固定流程整理当前客户文档',
  'intake.note': '{model}。用户只需开始一次；可选补充说明不会取消固定阶段或输出检查。',
  'intake.notesLabel': '补充说明（可选）', 'intake.notesPlaceholder': '例如：本次重点关注试验条件；不填写也会执行固定流程。',
  'intake.confirmMap': '先核对结构，再继续整理条目', 'intake.start': '开始固定流程',
  'intake.starting': '正在确认原请求…', 'intake.returnResult': '回到原任务成果', 'intake.notStarted': '尚未开始',
  'intake.found': '已找到原任务：{status}',
  'intake.runtimeAndBusiness': '运行状态与业务成果分别核对；已保存阶段不代表人工接受。',
  'intake.statusNotConfirmed': '原任务状态暂时无法确认，请保留当前说明并重试。',
  'intake.currentDocument': '当前客户文档：{name}',
  'intake.noStart': '当前宿主尚未配置本应用的任务启动能力；原件与已保存成果仍可核对。',
  'session.heading': 'PAIR 会话', 'session.connectionAria': 'PAIR 会话连接',
  'session.bound': '已关联原任务 · {status}',
  'session.unbound': '尚未开始；当前成果未关联此工作台保存的 PAIR 会话。',
  'session.runAndBusiness': '运行状态：{status}；业务成果状态单独以上述已保存阶段为准。',
  'hosted.setup.CAPABILITY_NOT_READY': '业务能力包尚未就绪：请检查配套安装和服务状态。',
  'hosted.setup.FIXED_WORKFLOW_UNAVAILABLE': '当前能力包不提供固定业务流程，请安装配套版本。',
  'hosted.setup.PAIR_DEFAULT_MODEL_UNAVAILABLE': 'PAIR 尚未提供可用默认模型：请在 PAIR 设置 → 模型中选择模型和推理档位。',
  'hosted.setup.PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED': '当前 PAIR 会话不支持模型选择，请更新 PAIR 宿主。',
  'hosted.setup.PAIR_SESSION_MODEL_UNAVAILABLE': 'PAIR 未能选择可用模型：请在 PAIR 设置 → 模型中选择可用模型和推理档位。',
  'hosted.setup.PAIR_SESSION_SERVICE_UNAVAILABLE': 'PAIR 会话服务不可用，请检查宿主服务。',
  'hosted.setup.TASK_START_NOT_CONFIGURED': 'PAIR 任务启动尚未配置：请确认能力包已安装并已选择默认模型。',
});
Object.assign(copy.en, {
  'workflow.noDescriptor': 'This host has not provided a fixed-workflow description; chat goals cannot replace stage checks.',
  'workflow.intakeAria': 'Fixed business workflow', 'workflow.defaultLabel': 'Customer requirement fixed workflow',
  'workflow.version': 'Version {version}', 'workflow.required': 'Required inputs', 'workflow.optional': 'Optional internal evidence',
  'workflow.expected': 'Expected outputs', 'workflow.customerSource': 'Customer requirement document',
  'workflow.available': 'Available', 'workflow.unavailable': 'Unavailable in this version',
  'workflow.noOptional': 'Project references and team knowledge are not connected in this version.',
  'workflow.expectedFallback': 'Document structure, source items and classification, self-check and a reviewable result.',
  'workflow.statusAria': 'Fixed workflow results', 'workflow.statusHeading': 'Business result stages',
  'workflow.statusUnconfirmed': 'Status unconfirmed',
  'workflow.unconfirmed': 'Fixed-workflow status is unconfirmed: {reason}. Saved results remain; re-read the task before reviewing them.',
  'workflow.noState': 'No fixed-workflow status is saved yet; the task has not started or results are not synchronized.',
  'workflow.unconfirmedReason': 'derived state does not match saved artifacts', 'workflow.workflowVersion': 'Workflow v{version}',
  'workflow.ready': 'Saved results meet the current review checks; human acceptance is still not claimed.', 'workflow.currentStage': 'Current: {stage}', 'workflow.viewDetails': 'View details', 'workflow.hideDetails': 'Hide details',
  'workflow.next': 'Next: ', 'workflow.continueRemaining': 'continue the remaining open items', 'workflow.openItems': 'Open items: ',
  'workflow.stage.PENDING': 'Not saved', 'workflow.stage.IN_PROGRESS': 'In progress', 'workflow.stage.SAVED': 'Saved',
  'workflow.stage.SKIPPED': 'Skipped by rule', 'workflow.stage.WAITING': 'Waiting for input',
  'workflow.stage.BLOCKED': 'Waiting for an earlier stage',
  'workflow.stageLabel.PREPARATION': 'Preparation', 'workflow.stageLabel.SOURCE_MAP': 'Source and structure',
  'workflow.stageLabel.ITEMS_CLASSIFICATION': 'Items, classification and explicit derivation',
  'workflow.stageLabel.EVIDENCE_ANALYSIS': 'Evidence analysis', 'workflow.stageLabel.SELF_CHECK': 'Self-check and correction',
  'workflow.stageLabel.DELIVERABLE': 'Reviewable result',
  'workflow.reason.SOURCE_MAP_REQUIRED': 'Save the document structure first',
  'workflow.reason.ITEMS_NOT_SAVED': 'Source items are not saved yet',
  'workflow.reason.ITEMS_REQUIRED': 'Save source items first',
  'workflow.reason.MAP_CONFIRMATION_REQUIRED': 'Waiting for this structure to be confirmed',
  'workflow.reason.SCOPED_DERIVATION_REMAINING': 'The authorized derivation scope is not complete',
  'workflow.reason.UNPROCESSED_SOURCE_SCOPE': 'Some source scope is still unprocessed',
  'workflow.reason.NO_INTERNAL_EVIDENCE': 'No internal evidence was provided',
  'workflow.reason.INTERNAL_EVIDENCE_NOT_WIRED': 'Internal evidence is not connected in this version',
  'workflow.reason.INTERNAL_EVIDENCE_ANALYSIS_REQUIRED': 'Internal-evidence analysis is not saved yet',
  'workflow.reason.WORKFLOW_EVIDENCE_CONFLICT': 'Workflow declaration conflicts with evidence state',
  'workflow.reason.SELF_CHECK_NOT_SAVED': 'No saved self-check yet',
  'workflow.reason.SELF_CHECK_NOT_RUN': 'The program self-check has not run',
  'workflow.reason.SELF_CHECK_BASIS_CHANGED': 'Self-check basis changed; run it again',
  'workflow.reason.SELF_CHECK_FAILED': 'Program self-check did not pass',
  'workflow.reason.DELIVERABLE_NOT_SAVED': 'No saved deliverable yet',
  'workflow.reason.SELF_CHECK_REQUIRED': 'Complete the self-check first',
  'workflow.reason.INCOMPLETE_OPERATION': 'An operation has an unknown outcome or awaits recovery',
  'workflow.reason.OPERATION_FAILURE': 'Failed attempt retained in history',
  'workflow.reason.EVIDENCE_ANALYSIS_SKIPPED': 'Evidence analysis skipped by declaration',
  'workflow.evidence.TASK_CONTEXT': 'Task and source', 'workflow.evidence.DOCUMENT_MAP': 'Document structure',
  'workflow.evidence.SOURCE_ITEMS': 'Source items', 'workflow.evidence.DECOMPOSITION': 'Explicit derivation',
  'workflow.evidence.REVIEW_HEAD': 'Self-check review', 'workflow.evidence.DELIVERY': 'Saved delivery',
  'workflow.evidence.WORKFLOW_BRANCH': 'Declared branch', 'workflow.evidence.WORKFLOW_CHECK': 'Program self-check',
  'workflow.evidence.DELIVERY_VERSION': 'Saved delivery version', 'workflow.evidence.TASK_BINDING': 'Task binding',
  'workflow.optional.project_references.label': 'Project references',
  'workflow.optional.project_references.reason': 'Project references are not connected in this version.',
  'workflow.optional.team_knowledge.label': 'Team knowledge',
  'workflow.optional.team_knowledge.reason': 'Team knowledge is not connected in this version.',
  'workflow.expected.structure': 'Document structure with traceable original and order',
  'workflow.expected.items': 'Source items, classification and authorized derivation with complete parents',
  'workflow.expected.evidence': 'Referenced analysis when internal evidence exists, otherwise explicitly marked as not run',
  'workflow.expected.check': 'Program self-check result and the current reviewable result version',
  'execution.ACCEPTED': 'Accepted', 'execution.RUNNING': 'Processing', 'execution.WAITING': 'Waiting',
  'execution.SUCCEEDED': 'This turn has ended', 'execution.FAILED': 'This turn did not complete',
  'execution.INTERRUPTED': 'Paused', 'execution.IDLE': 'Idle', 'execution.UNKNOWN': 'Status unconfirmed',
  'hosted.taskNoun': 'PAIR task', 'hosted.sessionNoun': 'PAIR session',
  'control.openSession': 'Open PAIR session', 'control.stop': 'Stop this turn',
  'control.continue': 'Continue this task', 'control.reconnect': 'Verify and reconnect this task',
  'intake.title': 'Organize this customer document with the fixed workflow',
  'intake.note': '{model}. Start once; optional notes cannot remove fixed stages or output checks.',
  'intake.notesLabel': 'Supplementary instructions (optional)',
  'intake.notesPlaceholder': 'For example: focus on test conditions; the fixed workflow still runs when this is empty.',
  'intake.confirmMap': 'Check the structure before items', 'intake.start': 'Start fixed workflow',
  'intake.starting': 'Confirming the original request…', 'intake.returnResult': 'Return to saved result',
  'intake.notStarted': 'Not started', 'intake.found': 'Found the original task: {status}',
  'intake.runtimeAndBusiness': 'Verify runtime and business results separately; saved stages do not claim human acceptance.',
  'intake.statusNotConfirmed': 'The original task status is temporarily unconfirmed; keep this note and retry.',
  'intake.currentDocument': 'Current customer document: {name}',
  'intake.noStart': 'This host has not configured task start for the application; the source and saved results remain reviewable.',
  'session.heading': 'PAIR session', 'session.connectionAria': 'PAIR session connection',
  'session.bound': 'Original task linked · {status}',
  'session.unbound': 'Not started; the current result is not linked to a PAIR session saved by this workbench.',
  'session.runAndBusiness': 'Runtime: {status}; business results remain governed by the saved stages above.',
  'hosted.setup.CAPABILITY_NOT_READY': 'The business capability package is not ready; check the matching installation and service.',
  'hosted.setup.FIXED_WORKFLOW_UNAVAILABLE': 'This capability package does not provide the fixed workflow; install the matching version.',
  'hosted.setup.PAIR_DEFAULT_MODEL_UNAVAILABLE': 'PAIR has no usable default model; choose a model and reasoning effort in PAIR Settings → Models.',
  'hosted.setup.PAIR_SESSION_MODEL_SELECTION_UNSUPPORTED': 'This PAIR session does not support model selection; update the PAIR host.',
  'hosted.setup.PAIR_SESSION_MODEL_UNAVAILABLE': 'PAIR could not select a usable model; choose one in PAIR Settings → Models.',
  'hosted.setup.PAIR_SESSION_SERVICE_UNAVAILABLE': 'The PAIR session service is unavailable; check the host service.',
  'hosted.setup.TASK_START_NOT_CONFIGURED': 'PAIR task start is not configured; confirm the capability package and default model.',
});
function key(origin) { return STORAGE + origin; }
function readSaved(origin) {
  try { const saved = JSON.parse(localStorage.getItem(key(origin))); return saved && validWorkSelection(saved.selection) ? saved : null; } catch { return null; }
}
function assetUrl(path, origin) {
  const url = new URL(path, origin);
  if (url.origin !== origin || !url.pathname.startsWith('/api/')) throw new Error('Unsupported ReqPilotX resource URL.');
  return PREFIX + '/service' + url.pathname + url.search;
}
function Panel({ t }) {
  const [origin, setOrigin] = React.useState('');
  const [saved, setSaved] = React.useState(null);
  const [value, setValue] = React.useState('');
  const [error, setError] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    const abort = new AbortController();
    fetch(PREFIX + '/config', { signal: abort.signal }).then(r => { if (!r.ok) throw Error(); return r.json(); }).then(config => {
      setOrigin(config.serviceOrigin); setSaved(readSaved(config.serviceOrigin));
    }).catch(() => { if (!abort.signal.aborted) setError(t('unavailable')); });
    return () => abort.abort();
  }, []);
  const host = React.useMemo(() => origin ? makeReadonlyHost(origin, selection => setSaved(previous => {
    const next = { ...previous, selection };
    try { localStorage.setItem(key(origin), JSON.stringify(next)); } catch { /* Reading remains available. */ }
    return next;
  })) : null, [origin]);
  async function open(event) {
    event.preventDefault(); if (!origin || busy) return; setBusy(true); setError('');
    try {
      const url = new URL(value);
      const q = url.searchParams;
      const selection = { projectId: q.get('projectId'), documentId: q.get('documentId'), workRef: q.get('pluginSessionId'), revisionId: q.get('resultRevision') || undefined, view: q.get('checkpoint') === 'list' ? 'items' : 'structure' };
      if (url.origin !== origin || url.pathname !== '/workspace' || !selection.projectId || !selection.documentId || !/^plugin-[a-f0-9]{32}$/.test(selection.workRef || '') || (selection.revisionId && !/^[a-f0-9]{64}$/.test(selection.revisionId))) throw Error(t('invalid'));
      const path = '/api/projects/' + encodeURIComponent(selection.projectId) + '/sor-documents/' + encodeURIComponent(selection.documentId) + '/agent-structure/delivery/plugin-workbench/' + selection.workRef;
      const response = await fetch(assetUrl(path, origin)); if (!response.ok) throw Error(t('unavailable'));
      const state = await response.json(); selection.sourceSha256 = state.source_sha256;
      if (!validWorkSelection(selection)) throw Error(t('invalid'));
      const next = { selection, documentName: state.file_name || state.document_name || 'ReqPilotX' };
      try { localStorage.setItem(key(origin), JSON.stringify(next)); } catch { /* Reading does not require storage permission. */ } setSaved(next);
    } catch (e) { setError(e.message || t('unavailable')); } finally { setBusy(false); }
  }
  return h('section', { className: 'reqpilotx-pair-frame' },
    saved && host ? h(React.Fragment, null,
      h('div', { className: 'reqpilotx-pair-switch' }, h('button', { onClick: () => { setSaved(null); try { localStorage.removeItem(key(origin)); } catch { /* Local selection only. */ } } }, t('change'))),
      h(ReqPilotXReadonlyPanel, { selection: saved.selection, documentName: saved.documentName, host, active: true, t })
    ) : h('form', { className: 'reqpilotx-pair-open', onSubmit: open },
      h('h1', null, t('intro')), h('p', null, t('hint')),
      h('label', null, t('link'), h('input', { type: 'url', required: true, value, onChange: e => setValue(e.target.value), 'aria-label': t('link') })),
      h('button', { type: 'submit', disabled: !origin || busy }, !origin ? t('loading') : t('open'))),
    error ? h('p', { role: 'alert' }, error) : null);
}
function Icon({ size }) {
  return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, 'aria-hidden': true },
    h('path', { d: 'M6 3h9l4 4v14H6z M14 3v5h5 M9 12h7 M9 16h7' }));
}
const inject = ['slots', 'locale', 'layout', 'sessions', 'conversation', 'uiWorkspace'];
function apply(ctx) {
  ctx.effect(() => ctx.locale.register(NS, copy), 'reqpilotx: dictionaries');
  ctx.effect(() => { const style = document.createElement('style'); style.dataset.reqpilotx = 'readonly'; style.textContent = panelCss; document.head.append(style); return () => style.remove(); }, 'reqpilotx: panel styles');
  ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'reqpilotx', locale: NS, inject: () => ({ openSession: async (sessionId, context) => {
    // View-only navigation belongs to the Workspace owner. It opens the same
    // Session without touching the composer, so no prompt or draft is inserted.
    if (!context) { ctx.uiWorkspace.openSession(sessionId); return; }
    const signal = ctx.layout.beginNavigation();
    try {
      await ctx.sessions.using(sessionId, { source: 'controllerOperation', signal }, async reference => {
        await reference.ready;
        signal.throwIfAborted();
        const scope = reference.binding.ctx;
        const input = ctx.conversation.input.for(scope);
        const state = input.state.getSnapshot();
        if (state.phase !== 'plain') throw Error('会话正在提交内容，请稍后再带入所选原文。');
        const returned = Object.fromEntries(['projectId', 'documentId', 'sourceSha256', 'workRef', 'revisionId', 'scope', 'nodeId', 'sourceItemId', 'derivedUnitId'].filter(key => context[key] !== undefined).map(key => [key, context[key]]));
        const text = '讨论当前选中的原文（保留此来源与版本）：\n' + JSON.stringify(returned, null, 2) + '\n\n';
        const draft = state.draft.length ? '\n\n' + text : text;
        if (scope.bail(scope, 'slash/input-insert-text', { text: draft, span: { start: state.draft.length, end: state.draft.length, draftRev: state.draftRev } }) !== true) throw Error('当前会话未接收原文上下文，请重试。');
        signal.throwIfAborted();
        ctx.uiWorkspace.openSession(sessionId);
      });
    } catch (error) {
      if (signal.aborted) throw Error('页面已切换到另一项工作，未插入所选原文。');
      throw error;
    }
  } }) }, Application));
  ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: 'reqpilotx', label: 'ReqPilotX', order: 20 }, Icon));
}
module.exports = { inject, apply };
