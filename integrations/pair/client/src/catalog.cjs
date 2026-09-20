// Native project/document entry. All business data stays in the shared ReqPilotX API.
Object.assign(copy.zh, { projects: '项目与文档', newProject: '新建项目', projectName: '项目名称', customer: '客户名称', create: '创建项目', cancel: '取消', choose: '选择项目', noProjects: '新建一个项目，开始整理客户文档。', upload: '导入客户文档（PDF）', noDocs: '项目中还没有客户文档。', documents: '客户文档', back: '返回文档', original: '原文', results: '整理成果', noResults: '原文已保存，尚无整理成果。', opening: '正在读取…', saved: '原文已保存', unavailableRuntime: 'ReqPilotX 暂不可用', work: '打开成果', ready: '服务已就绪' });
Object.assign(copy.en, { projects: 'Projects and documents', newProject: 'New project', projectName: 'Project name', customer: 'Customer name', create: 'Create project', cancel: 'Cancel', choose: 'Select project', noProjects: 'Create a project to organize customer documents.', upload: 'Import customer document (PDF)', noDocs: 'No customer documents in this project yet.', documents: 'Customer documents', back: 'Back to documents', original: 'Source document', results: 'Saved work', noResults: 'Source document saved. No extraction result yet.', opening: 'Loading…', saved: 'Source saved', unavailableRuntime: 'ReqPilotX is unavailable', work: 'Open result', ready: 'Service ready' });
function makeReadonlyHost(origin, onSelectionChange) {
  return { id: 'pair:' + origin, label: 'Pair', client: createReadonlyHttpClient('pair:' + origin, path => assetUrl(path, origin)), resourceUrl: path => assetUrl(path, origin), onSelectionChange,
    pdfViewerUrl: (source, page, embedded) => {
      const parsed = new URL(source, window.location.origin);
      const original = parsed.pathname.startsWith(PREFIX + '/service/api/') ? new URL(parsed.pathname.slice((PREFIX + '/service').length) + parsed.search, origin) : new URL(source, origin);
      if (original.origin !== origin || !original.pathname.startsWith('/api/')) throw Error('Unsupported PDF source.');
      const viewer = new URL('/pdf-viewer', origin); viewer.searchParams.set('pdf', original.href); viewer.searchParams.set('page', String(page));
      if (embedded) { viewer.searchParams.set('embedded', '1'); viewer.searchParams.set('parentOrigin', window.location.origin); }
      return viewer.href;
    } };
}
function Application({ t }) {
  const [config, setConfig] = React.useState(null);
  React.useEffect(() => {
    let disposed = false; const abort = new AbortController(); let timer;
    async function poll() {
      try { const response = await fetch(PREFIX + '/config', { signal: abort.signal }); if (!response.ok) throw Error(); const next = await response.json(); if (!disposed) setConfig(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next); }
      catch { if (!disposed) setConfig({ status: 'ERROR', message: t('unavailableRuntime') }); }
      if (!disposed) timer = setTimeout(poll, 2000);
    }
    void poll(); return () => { disposed = true; abort.abort(); clearTimeout(timer); };
  }, []);
  if (!config || config.status !== 'READY') return h('section', { className: 'reqpilotx-pair-home' }, h('h1', null, 'ReqPilotX'), h('p', { role: config?.status === 'ERROR' ? 'alert' : 'status' }, config?.message || t('loading')));
  return config.mode === 'managed' ? h(Catalog, { config, t, key: config.instanceId }) : h(Panel, { t });
}
function Catalog({ config, t }) {
  const [projects, setProjects] = React.useState([]), [projectId, setProjectId] = React.useState('');
  const [documents, setDocuments] = React.useState([]), [document, setDocument] = React.useState(null);
  const [creating, setCreating] = React.useState(false), [name, setName] = React.useState(''), [customer, setCustomer] = React.useState('');
  const [busy, setBusy] = React.useState(false), [error, setError] = React.useState(''), [works, setWorks] = React.useState([]), [selection, setSelection] = React.useState(null);
  const origin = config.serviceOrigin;
  const request = React.useCallback((path, options) => requestWithTransport(fetch, assetUrl(path, origin), options), [origin]);
  const client = React.useMemo(() => createProjectDocumentClient(request), [request]);
  React.useEffect(() => { let live = true; client.projects().then(items => { if (live) setProjects(items); }).catch(e => { if (live) setError(e.message); }); return () => { live = false; }; }, [request]);
  React.useEffect(() => {
    setDocuments([]); setDocument(null); setSelection(null); if (!projectId) return;
    let live = true; client.sorDocuments(projectId).then(items => { if (live) setDocuments(items); }).catch(e => { if (live) setError(e.message); }); return () => { live = false; };
  }, [projectId, request]);
  React.useEffect(() => {
    setWorks([]); setSelection(null); if (!document) return; let live = true;
    request(projectDocumentPaths.works(projectId, document.id)).then(value => { if (live) setWorks(value.sessions); }).catch(e => { if (live) setError(e.message); }); return () => { live = false; };
  }, [document, projectId, request]);
  const host = React.useMemo(() => makeReadonlyHost(origin, setSelection), [origin]);
  async function create(event) {
    event.preventDefault(); if (busy) return; setBusy(true); setError('');
    try { const project = await client.createProject(name.trim(), customer.trim()); setProjects(await client.projects()); setProjectId(project.id); setCreating(false); setName(''); setCustomer(''); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  async function upload(event) {
    const input = event.currentTarget, file = input.files?.[0]; if (!file || busy) return; setBusy(true); setError('');
    try { const created = await client.uploadSorDocument(projectId, file); setDocuments(await client.sorDocuments(projectId)); setDocument(created); }
    catch (e) { setError(e.message); } finally { setBusy(false); input.value = ''; }
  }
  const pdf = document ? host.pdfViewerUrl(new URL(projectDocumentPaths.source(projectId, document.id), origin).href, 1, true) : '';
  return h('section', { className: 'reqpilotx-pair-home' },
    h('header', { className: 'reqpilotx-pair-home-header' }, h('div', null, h('h1', null, 'ReqPilotX'), h('p', null, t('projects'))), h('span', { role: 'status' }, busy ? t('opening') : t('ready'))),
    error && h('p', { role: 'alert' }, error),
    h('div', { className: 'reqpilotx-pair-toolbar' }, h('label', null, t('choose'), h('select', { value: projectId, disabled: busy, onChange: e => setProjectId(e.target.value) }, h('option', { value: '' }, t('choose')), projects.map(p => h('option', { key: p.id, value: p.id }, p.name)))), h('button', { onClick: () => setCreating(!creating), disabled: busy }, t('newProject'))),
    creating && h('form', { onSubmit: create, className: 'reqpilotx-pair-project-form' }, h('label', null, t('projectName'), h('input', { required: true, maxLength: 255, value: name, onChange: e => setName(e.target.value) })), h('label', null, t('customer'), h('input', { required: true, maxLength: 255, value: customer, onChange: e => setCustomer(e.target.value) })), h('button', { disabled: busy || !name.trim() || !customer.trim() }, t('create'))),
    !projects.length && !creating && h('p', null, t('noProjects')),
    projectId && h('div', { className: 'reqpilotx-pair-document-area' }, h('label', { className: 'reqpilotx-pair-import' }, t('upload'), h('input', { type: 'file', accept: '.pdf,application/pdf', disabled: busy, onChange: upload, 'aria-label': t('upload') })),
      !document ? h(React.Fragment, null, !documents.length && h('p', null, t('noDocs')), h('ul', { className: 'reqpilotx-pair-documents' }, documents.map(d => h('li', { key: d.id }, h('button', { onClick: () => setDocument(d) }, d.file_name), h('span', null, t('saved'))))))
      : h(React.Fragment, null, h('div', { className: 'reqpilotx-pair-toolbar' }, h('button', { onClick: () => { setDocument(null); setSelection(null); } }, t('back')), h('strong', null, document.file_name)),
          selection ? h(ReqPilotXReadonlyPanel, { selection, documentName: document.file_name, host, active: true })
          : h('div', { className: 'reqpilotx-pair-document' }, h('section', null, h('h2', null, t('results')), !works.length && h('p', null, t('noResults')), works.map(work => h('button', { key: work.session_id, disabled: !work.revision_id, onClick: () => setSelection({ projectId, documentId: document.id, sourceSha256: document.source_sha256, workRef: work.session_id, revisionId: work.revision_id, view: 'structure' }) }, t('work') + ' · v' + work.revision_number))), h('iframe', { title: t('original'), src: pdf })))));
}
