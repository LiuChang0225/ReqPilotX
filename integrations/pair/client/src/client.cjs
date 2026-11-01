// Wrapped at build time with the shared panel CJS module and host-provided require.
const React = require('react');
const { ReqPilotXReadonlyPanel, createReadonlyHttpClient, validWorkSelection, createProjectDocumentClient, projectDocumentPaths, requestWithTransport } = panelModule.exports;
const h = React.createElement;
const PREFIX = '/reqpilotx';
const STORAGE = 'reqpilotx:readonly-selection:v1:';
const NS = 'reqpilotx-panel';
const copy = {
  zh: { title: 'ReqPilotX', intro: '打开已保存的需求成果', hint: '粘贴包含具体工作与版本的 ReqPilotX 工作台链接。', link: '工作台链接', open: '打开成果', change: '切换工作', loading: '正在连接 ReqPilotX…', invalid: '请提供当前服务中包含项目、文档和具体工作的工作台链接。', unavailable: '无法读取该工作，请检查链接和服务连接。' },
  en: { title: 'ReqPilotX', intro: 'Open saved requirement work', hint: 'Paste a ReqPilotX workbench link with its work and revision.', link: 'Workbench link', open: 'Open work', change: 'Change work', loading: 'Connecting to ReqPilotX…', invalid: 'Use a workbench link for this service with a project, document and explicit work.', unavailable: 'Could not read this work. Check the link and service connection.' },
};
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
      h(ReqPilotXReadonlyPanel, { selection: saved.selection, documentName: saved.documentName, host, active: true })
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
const inject = ['slots', 'locale', 'layout', 'sessions', 'conversation'];
function apply(ctx) {
  ctx.effect(() => ctx.locale.register(NS, copy), 'reqpilotx: dictionaries');
  ctx.effect(() => { const style = document.createElement('style'); style.dataset.reqpilotx = 'readonly'; style.textContent = panelCss; document.head.append(style); return () => style.remove(); }, 'reqpilotx: panel styles');
  ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'reqpilotx', locale: NS, inject: () => ({ openSession: async (sessionId, context) => {
    const signal = ctx.layout.beginNavigation(); await ctx.sessions.open(sessionId); if (signal.aborted) return;
    if (context) {
      const scope = ctx.sessions.scope(sessionId); if (!scope) throw Error('关联会话尚未就绪。');
      const state = ctx.conversation.input.for(scope).state.getSnapshot();
      if (state.phase !== 'plain') throw Error('会话正在提交内容，请稍后再带入所选原文。');
      const reference = Object.fromEntries(['projectId', 'documentId', 'sourceSha256', 'workRef', 'revisionId', 'scope', 'nodeId', 'sourceItemId', 'derivedUnitId'].filter(key => context[key] !== undefined).map(key => [key, context[key]]));
      const text = '讨论当前选中的原文（保留此来源与版本）：\n' + JSON.stringify(reference, null, 2) + '\n\n';
      if (scope.bail(scope, 'slash/input-insert-text', { text, span: { start: 0, end: 0, draftRev: state.draftRev } }) !== true) throw Error('当前会话未接收原文上下文，请重试。');
    }
    ctx.layout.selectPanel(null);
  } }) }, Application));
  ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: 'reqpilotx', label: 'ReqPilotX', order: 20 }, Icon));
}
module.exports = { inject, apply };
