const state = {
  projectId: "",
  documentId: "",
  projects: [],
  documents: [],
  workspace: null,
  preview: null,
  reviewEntries: [],
  reviewEntriesRaw: [],
  selectedReq: {},
  showSelectedOnly: false,
  showRecommendedSourceOnly: false,
  showRawDuplicateRows: false,
  selectedKinds: new Set(),
  rawViewByEntryId: {},
  pdfBaseUrl: "",
  pdfCurrentPage: 1,
  activeEntryId: "",
  pdfViewerReady: false,
  pendingPdfCommand: null,
  pdfDrawerOpen: false,
  tableViewByTableId: {},
};
const WORKSPACE_SELECTION_KEY = "reqpilotx.workspace-selection-v1";
const FRONTEND_BUILD = "20260310-2";
const PDF_VIEWER_VERSION = "20260403-reqx1";

const el = {
  projectSelect: document.querySelector("#project-select"),
  documentSelect: document.querySelector("#document-select"),
  refreshBtn: document.querySelector("#refresh-btn"),
  loadBtn: document.querySelector("#load-btn"),
  summaryCards: document.querySelector("#summary-cards"),
  statusLine: document.querySelector("#status-line"),
  engineBody: document.querySelector("#engine-body"),
  sectionOverviewBody: document.querySelector("#section-overview-body"),
  consistencyBody: document.querySelector("#consistency-body"),
  requirementBody: document.querySelector("#requirement-body"),
  selectAllBtn: document.querySelector("#select-all-btn"),
  unselectAllBtn: document.querySelector("#unselect-all-btn"),
  toggleSelectedBtn: document.querySelector("#toggle-selected-btn"),
  toggleSourceRecommendedBtn: document.querySelector("#toggle-source-recommended-btn"),
  applyReviewedBtn: document.querySelector("#apply-reviewed-btn"),
  lockReviewedBtn: document.querySelector("#lock-reviewed-btn"),
  selectionStatus: document.querySelector("#selection-status"),
  rawRowsToggle: document.querySelector("#raw-rows-toggle"),
  typeFilterGroup: document.querySelector("#type-filter-group"),
  sectionFilter: document.querySelector("#section-filter"),
  keywordFilter: document.querySelector("#keyword-filter"),
  openPdfBtn: document.querySelector("#open-pdf-btn"),
  pdfDrawerToggle: document.querySelector("#pdf-drawer-toggle"),
  pdfViewerFrame: document.querySelector("#pdf-viewer-frame"),
  pdfHint: document.querySelector("#pdf-hint"),
  reviewPage: document.querySelector(".review-page"),
};

boot().catch((error) => {
  console.error(error);
  alert(error.message || "页面初始化失败。");
});

async function boot() {
  restoreWorkspaceSelection();
  bindEvents();
  syncPdfDrawerState();
  resetPdfViewer("点击左侧条目后，这里会跳转到对应页。");
  await loadProjects();
}

function restoreWorkspaceSelection() {
  try {
    const payload = JSON.parse(window.localStorage.getItem(WORKSPACE_SELECTION_KEY) || "{}");
    if (payload && typeof payload === "object") {
      state.projectId = payload.projectId || "";
      state.documentId = payload.documentId || "";
    }
  } catch (_error) {
    state.projectId = state.projectId || "";
    state.documentId = state.documentId || "";
  }
}

function persistWorkspaceSelection() {
  try {
    window.localStorage.setItem(
      WORKSPACE_SELECTION_KEY,
      JSON.stringify({
        projectId: state.projectId || "",
        documentId: state.documentId || "",
      })
    );
  } catch (_error) {
    // Best effort only.
  }
}

function bindEvents() {
  el.refreshBtn.addEventListener("click", wrap(onRefresh));
  el.loadBtn.addEventListener("click", wrap(onLoadPreview));
  el.projectSelect.addEventListener("change", wrap(onProjectChanged));
  el.documentSelect.addEventListener("change", onDocumentChanged);
  el.sectionFilter.addEventListener("input", renderFilteredRequirements);
  el.keywordFilter.addEventListener("input", renderFilteredRequirements);
  el.selectAllBtn.addEventListener("click", () => bulkSelect(true));
  el.unselectAllBtn.addEventListener("click", () => bulkSelect(false));
  el.toggleSelectedBtn.addEventListener("click", toggleSelectedOnly);
  if (el.toggleSourceRecommendedBtn) {
    el.toggleSourceRecommendedBtn.addEventListener("click", toggleRecommendedSourceOnly);
  }
  if (el.applyReviewedBtn) {
    el.applyReviewedBtn.addEventListener("click", wrap(onApplyReviewedExtraction));
  }
  if (el.lockReviewedBtn) {
    el.lockReviewedBtn.addEventListener("click", wrap(onLockReviewedExtraction));
  }
  if (el.rawRowsToggle) {
    el.rawRowsToggle.addEventListener("change", onRawRowsToggleChanged);
  }
  el.requirementBody.addEventListener("change", onRequirementSelectionChanged);
  el.requirementBody.addEventListener("click", wrap(onRequirementTableClicked));
  if (el.openPdfBtn) {
    el.openPdfBtn.addEventListener("click", openPdfInNewWindow);
  }
  if (el.pdfDrawerToggle) {
    el.pdfDrawerToggle.addEventListener("click", () => {
      setPdfDrawerOpen(!state.pdfDrawerOpen);
    });
  }
  window.addEventListener("resize", syncPdfDrawerState);
  window.addEventListener("keydown", onGlobalKeyDown);
  window.addEventListener("message", onPdfViewerMessage);
}

function onGlobalKeyDown(event) {
  if (event.key !== "Escape") {
    return;
  }
  if (!state.pdfDrawerOpen) {
    return;
  }
  setPdfDrawerOpen(false);
}

function isDesktopDrawerMode() {
  return window.matchMedia("(min-width: 1200px)").matches;
}

function syncPdfDrawerState() {
  if (!isDesktopDrawerMode()) {
    state.pdfDrawerOpen = false;
  }
  applyPdfDrawerClass();
  refreshPdfDrawerToggle();
}

function setPdfDrawerOpen(nextOpen) {
  if (!state.pdfBaseUrl) {
    state.pdfDrawerOpen = false;
  } else if (isDesktopDrawerMode()) {
    state.pdfDrawerOpen = Boolean(nextOpen);
  } else {
    state.pdfDrawerOpen = false;
  }
  applyPdfDrawerClass();
  refreshPdfDrawerToggle();
}

function applyPdfDrawerClass() {
  if (!el.reviewPage) {
    return;
  }
  const shouldOpen = state.pdfDrawerOpen && isDesktopDrawerMode();
  el.reviewPage.classList.toggle("pdf-drawer-open", shouldOpen);
}

function refreshPdfDrawerToggle() {
  if (!el.pdfDrawerToggle) {
    return;
  }
  const enabled = Boolean(state.pdfBaseUrl) && isDesktopDrawerMode();
  el.pdfDrawerToggle.disabled = !enabled;
  const expanded = enabled && state.pdfDrawerOpen;
  el.pdfDrawerToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  el.pdfDrawerToggle.textContent = expanded ? "收起对照" : "展开对照";
}

async function onRefresh() {
  await loadProjects();
}

async function onProjectChanged() {
  state.projectId = el.projectSelect.value;
  state.documentId = "";
  state.workspace = null;
  state.preview = null;
  state.tableViewByTableId = {};
  persistWorkspaceSelection();
  await loadDocuments();
  clearRendered();
}

function onDocumentChanged() {
  state.documentId = el.documentSelect.value;
  state.preview = null;
  state.tableViewByTableId = {};
  persistWorkspaceSelection();
  clearRendered();
}

async function onLoadPreview() {
  if (!state.projectId) {
    alert("请先选择项目");
    return;
  }
  if (!state.documentId) {
    alert("请先选择 SOR 文档");
    return;
  }
  el.statusLine.textContent = `加载并解析中（前端 ${FRONTEND_BUILD}），请稍候...`;
  const data = await api(
    `/api/projects/${state.projectId}/sor-documents/${state.documentId}/workspace?include_preview=1`
  );
  state.workspace = data;
  state.preview = data.preview || null;
  initPdfViewer(data.document?.file_name || data.file_name || "");
  renderPreview(data);
}

async function loadProjects() {
  const projects = await api("/api/projects");
  state.projects = projects;
  renderProjectOptions();
  if (state.projectId) {
    await loadDocuments();
  }
}

function renderProjectOptions() {
  const current = state.projectId;
  el.projectSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.projects.length ? "请选择项目" : "暂无项目";
  el.projectSelect.appendChild(placeholder);

  state.projects.forEach((project) => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name} / ${project.customer_name}`;
    if (project.id === current) {
      option.selected = true;
    }
    el.projectSelect.appendChild(option);
  });

  if (!state.projectId && state.projects.length) {
    state.projectId = state.projects[0].id;
    el.projectSelect.value = state.projectId;
    persistWorkspaceSelection();
  }
}

async function loadDocuments() {
  if (!state.projectId) {
    state.documents = [];
    renderDocumentOptions();
    return;
  }
  state.documents = await api(`/api/projects/${state.projectId}/sor-documents`);
  renderDocumentOptions();
}

function renderDocumentOptions() {
  const current = state.documentId;
  el.documentSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.documents.length ? "请选择 SOR 文档" : "当前项目暂无 SOR 文档";
  el.documentSelect.appendChild(placeholder);

  state.documents.forEach((doc) => {
    const option = document.createElement("option");
    option.value = doc.id;
    const shortId = String(doc.id || "").slice(0, 8);
    const created = formatTime(doc.created_at || "");
    option.textContent = `${doc.file_name} [${shortId}]${created ? ` ${created}` : ""}`;
    if (doc.id === current) {
      option.selected = true;
    }
    el.documentSelect.appendChild(option);
  });

  if (!state.documentId && state.documents.length) {
    state.documentId = state.documents[0].id;
    el.documentSelect.value = state.documentId;
  }
  persistWorkspaceSelection();
}

function clearRendered() {
  state.workspace = null;
  el.summaryCards.innerHTML = "";
  el.engineBody.innerHTML = "";
  el.sectionOverviewBody.innerHTML = "";
  el.consistencyBody.innerHTML = "";
  el.requirementBody.innerHTML = "";
  el.statusLine.textContent = "";
  if (el.typeFilterGroup) {
    el.typeFilterGroup.innerHTML = "";
  }
  state.reviewEntries = [];
  state.reviewEntriesRaw = [];
  state.selectedReq = {};
  state.showSelectedOnly = false;
  state.showRecommendedSourceOnly = false;
  state.showRawDuplicateRows = false;
  state.showRecommendedSourceOnly = false;
  state.selectedKinds = new Set();
  state.rawViewByEntryId = {};
  state.activeEntryId = "";
  state.pdfBaseUrl = "";
  state.pdfCurrentPage = 1;
  state.pdfViewerReady = false;
  state.pendingPdfCommand = null;
  state.pdfDrawerOpen = false;
  syncSelectionButtons();
  if (el.rawRowsToggle) {
    el.rawRowsToggle.checked = false;
  }
  if (el.selectionStatus) {
    el.selectionStatus.textContent = "";
  }
  if (el.lockReviewedBtn) {
    el.lockReviewedBtn.disabled = true;
  }
  syncPdfDrawerState();
  resetPdfViewer("点击左侧条目后，这里会跳转到对应页。");
}

function initPdfViewer(fileName) {
  const isPdf = String(fileName || "")
    .trim()
    .toLowerCase()
    .endsWith(".pdf");
  if (!isPdf) {
    state.pdfBaseUrl = "";
    state.pdfCurrentPage = 1;
    state.pdfViewerReady = false;
    state.pendingPdfCommand = null;
    state.pdfDrawerOpen = false;
    syncPdfDrawerState();
    resetPdfViewer("当前文档不是 PDF，右侧仅支持 PDF 原文对照。");
    return;
  }
  state.pdfBaseUrl = `/api/projects/${state.projectId}/sor-documents/${state.documentId}/file`;
  state.pdfCurrentPage = 1;
  state.pdfViewerReady = false;
  state.pdfDrawerOpen = false;
  syncPdfDrawerState();
  loadPdfPage(1, true, "");
}

function resetPdfViewer(message) {
  if (el.pdfViewerFrame) {
    el.pdfViewerFrame.removeAttribute("src");
  }
  state.pdfViewerReady = false;
  state.pendingPdfCommand = null;
  if (el.pdfHint) {
    el.pdfHint.textContent = message || "点击左侧条目后，这里会跳转到对应页。";
  }
  if (el.openPdfBtn) {
    el.openPdfBtn.disabled = !state.pdfBaseUrl;
  }
  refreshPdfDrawerToggle();
}

function loadPdfPage(pageNo, forceReload = false, highlightText = "") {
  if (!state.pdfBaseUrl) {
    return;
  }
  const safePage = Number(pageNo || 1) > 0 ? Number(pageNo || 1) : 1;
  const normalizedHighlight = String(highlightText || "").slice(0, 1800);
  const hasViewerSrc = Boolean(el.pdfViewerFrame?.src);
  const hasViewerLoaded = String(el.pdfViewerFrame?.src || "").includes("/assets/pdf-viewer.html");

  if (!forceReload && safePage === state.pdfCurrentPage && hasViewerSrc) {
    if (state.pdfViewerReady && hasViewerLoaded) {
      sendPdfViewerCommand(safePage, normalizedHighlight);
    } else {
      state.pendingPdfCommand = { page: safePage, highlightText: normalizedHighlight };
    }
    return;
  }

  state.pdfCurrentPage = safePage;
  const viewerUrl = buildPdfViewerUrl(safePage);
  if (el.pdfViewerFrame) {
    if (!forceReload && hasViewerLoaded && state.pdfViewerReady && el.pdfViewerFrame.contentWindow) {
      sendPdfViewerCommand(safePage, normalizedHighlight);
    } else {
      state.pdfViewerReady = false;
      state.pendingPdfCommand = { page: safePage, highlightText: normalizedHighlight };
      el.pdfViewerFrame.src = viewerUrl;
    }
  }
  if (el.pdfHint) {
    el.pdfHint.textContent = `正在对照第 ${safePage} 页（点击条目可重新定位）。`;
  }
  if (el.openPdfBtn) {
    el.openPdfBtn.disabled = false;
  }
}

function openPdfInNewWindow() {
  if (!state.pdfBaseUrl) {
    return;
  }
  const pageNo = Number(state.pdfCurrentPage || 1);
  const url = `${state.pdfBaseUrl}#page=${pageNo}&zoom=page-fit`;
  window.open(url, "_blank", "noopener,noreferrer");
}

function buildPdfViewerUrl(pageNo) {
  const encodedPdf = encodeURIComponent(state.pdfBaseUrl || "");
  return `/assets/pdf-viewer.html?v=${PDF_VIEWER_VERSION}&pdf=${encodedPdf}&page=${Number(pageNo || 1)}`;
}

function onPdfViewerMessage(event) {
  if (!el.pdfViewerFrame || event.source !== el.pdfViewerFrame.contentWindow) {
    return;
  }
  const data = event.data || {};
  if (data.type === "pdf-viewer-ready") {
    state.pdfViewerReady = true;
    if (state.pendingPdfCommand) {
      sendPdfViewerCommand(state.pendingPdfCommand.page, state.pendingPdfCommand.highlightText || "");
      state.pendingPdfCommand = null;
    } else {
      sendPdfViewerCommand(state.pdfCurrentPage, "");
    }
    return;
  }
  if (data.type === "pdf-page-changed") {
    const nextPage = Number(data.page || 0);
    if (nextPage > 0) {
      state.pdfCurrentPage = nextPage;
      if (el.pdfHint) {
        el.pdfHint.textContent = `正在对照第 ${nextPage} 页（点击条目可重新定位）。`;
      }
    }
  }
}

function sendPdfViewerCommand(pageNo, highlightText = "") {
  if (!el.pdfViewerFrame?.contentWindow) {
    return;
  }
  const command = {
    type: "jump-page",
    page: Number(pageNo || 1),
    highlightText: String(highlightText || ""),
  };
  el.pdfViewerFrame.contentWindow.postMessage(command, "*");
}

function renderPreview(data) {
  state.reviewEntries = buildReviewEntries(data, false);
  state.reviewEntriesRaw = buildReviewEntries(data, true);
  state.showRawDuplicateRows = false;
  state.selectedKinds = new Set();
  state.rawViewByEntryId = {};
  if (el.rawRowsToggle) {
    el.rawRowsToggle.checked = false;
  }
  initRequirementSelection(state.reviewEntries);
  renderSummary(data);
  renderEngineTable(data.preview?.summary?.selected_engine_pages || {});
  renderSectionOverviewTable(
    buildSectionOverviewRows(data.preview?.section_tree || data.preview?.sections || data.preview?.section_outline || [])
  );
  renderConsistencyTable(data.preview?.section_consistency || {});
  renderTypeFilter(state.reviewEntries);
  syncSelectionButtons();
  syncWorkspaceStageActions();
  renderFilteredRequirements();
}

function renderSummary(data) {
  const preview = data.preview || {};
  const summary = preview.summary || {};
  const document = data.document || {};
  const project = data.project || {};
  const workspaceSummary = data.summary || {};
  const cards = [
    ["文档", document.file_name || preview.file_name || ""],
    ["工作流", formatWorkflowStage(document.workflow_stage)],
    ["条目集版本", document.requirement_set_version ?? 0],
    ["AI 状态", formatAiStatus(document.ai_status, document.ai_stale)],
    ["知识库版本", project.knowledge_base_version ?? 0],
    ["知识文档", workspaceSummary.knowledge_doc_count ?? 0],
    ["融合模式", summary.merge_mode || "-"],
    ["矩阵条数", workspaceSummary.requirement_count ?? 0],
    ["需求条数", summary.requirements_count ?? 0],
    ["完整条目", summary.complete_entries_count ?? 0],
    ["原始条目", summary.complete_entries_raw_count ?? 0],
    ["章节数", summary.sections_count ?? 0],
    ["章节树节点", summary.section_tree_count ?? summary.section_outline_count ?? 0],
    ["原始 Outline", summary.section_outline_count ?? 0],
    ["顺序模型", summary.section_order_model || "-"],
    ["章节一致性问题", summary.section_consistency_issues_total ?? 0],
    ["零直挂父章", summary.zero_direct_requirement_parent_count ?? 0],
    ["Synthetic 章节", summary.synthetic_section_count ?? 0],
    ["Orphan 章节", summary.orphan_section_count ?? 0],
    ["需求覆盖页", summary.pages_with_requirements ?? 0],
    ["表格页", summary.table_pages ?? 0],
    ["表格注入", summary.table_block_injected_count ?? 0],
    ["行注入", summary.line_injection_count ?? 0],
    ["行重排", summary.line_reorder_count ?? 0],
    ["低置信页", summary.low_confidence_page_count ?? 0],
    ["精确去重", summary.dedup_exact_count ?? 0],
    ["前缀增量", summary.dedup_prefix_count ?? 0],
    ["HYBRID", summary.hybrid_entries_count ?? 0],
    ["巨型行清理", summary.dropped_mega_table_rows ?? 0],
    ["水印去除", summary.removed_watermark_total ?? 0],
  ];
  el.summaryCards.innerHTML = cards
    .map(
      ([label, value]) => `
      <article class="card">
        <div class="label">${escapeHtml(label)}</div>
        <div class="value">${escapeHtml(String(value))}</div>
      </article>
    `
    )
    .join("");
  const shortDocId = String(document.id || "").slice(0, 8) || "-";
  const generatedAt = formatTime(data.preview_generated_at || "");
  el.statusLine.textContent = `工作区已加载：${escapeHtml(document.file_name || preview.file_name || "")} [${shortDocId}]，阶段 ${formatWorkflowStage(
    document.workflow_stage
  )}，AI ${formatAiStatus(document.ai_status, document.ai_stale)}。诊断快照${
    generatedAt ? ` @ ${generatedAt}` : ""
  }，章节节点 ${
    summary.section_outline_count || 0
  }，需求条目 ${summary.requirements_count || 0}，一致性问题 ${summary.section_consistency_issues_total || 0}，低置信页 ${summary.low_confidence_page_count || 0}，顺序模型 ${summary.section_order_model || "-"}.`;
}

function renderEngineTable(engineMap) {
  const entries = Object.entries(engineMap || {});
  if (!entries.length) {
    el.engineBody.innerHTML = `<tr><td colspan="2">无引擎分布数据</td></tr>`;
    return;
  }
  el.engineBody.innerHTML = entries
    .sort((a, b) => b[1] - a[1])
    .map(([engine, count]) => `<tr><td>${escapeHtml(engine)}</td><td>${count}</td></tr>`)
    .join("");
}

function buildSectionOverviewLookupKey(scope, code) {
  const normalizedScope = String(scope || "").trim().toUpperCase();
  const normalizedCode = String(code || "").trim();
  return normalizedCode ? `${normalizedScope}:${normalizedCode}` : "";
}

function buildSectionOverviewNodeLabel(row) {
  if (!String(row.section_code || row.owner_section_code || "").trim() && row.is_appendix_scope_root) {
    return "ROOT";
  }
  const explicitLabel = String(row.owner_section_label || row.section_label || row.section_code || "").trim();
  const sectionCode = String(row.section_code || row.owner_section_code || "").trim();
  const sectionTitle = String(row.section_title || row.owner_section_title || "").trim();
  if (explicitLabel && sectionTitle) {
    const compactLabel = explicitLabel.replace(/\s+/g, "");
    const compactTitle = sectionTitle.replace(/\s+/g, "");
    if (compactLabel.includes(compactTitle)) {
      return explicitLabel;
    }
    return `${explicitLabel} ${sectionTitle}`.trim();
  }
  return explicitLabel || [sectionCode, sectionTitle].filter(Boolean).join(" ").trim() || sectionTitle;
}

function buildSectionOverviewParentLabel(row) {
  const parentLabel = String(row.structural_parent_section_label || row.parent_section_label || "").trim();
  const parentCode = String(row.structural_parent_section_code || row.parent_section_code || "").trim();
  const parentTitle = String(row.structural_parent_section_title || row.parent_section_title || "").trim();
  if (parentLabel && parentTitle) {
    const compactLabel = parentLabel.replace(/\s+/g, "");
    const compactTitle = parentTitle.replace(/\s+/g, "");
    if (compactLabel.includes(compactTitle)) {
      return parentLabel;
    }
    return `${parentLabel} ${parentTitle}`.trim();
  }
  return parentLabel || [parentCode, parentTitle].filter(Boolean).join(" ").trim();
}

function buildSectionOverviewTitleMeta(row) {
  if (!row?.is_appendix_scope_root) {
    return "";
  }
  const nodeLabel = String(row.node_label || buildSectionOverviewNodeLabel(row) || "").trim();
  const sectionTitle = String(row.section_title || "").trim();
  if (!sectionTitle) {
    return "";
  }
  const compactNode = nodeLabel.replace(/\s+/g, "");
  const compactTitle = sectionTitle.replace(/\s+/g, "");
  if (!compactTitle) {
    return "";
  }
  if (compactNode === compactTitle || compactNode.includes(compactTitle)) {
    return "";
  }
  return sectionTitle;
}

function renderSectionOverviewHead(bodyEl, levelCount, trailingHeaders) {
  const head = bodyEl?.closest("table")?.querySelector("thead");
  if (!head) {
    return;
  }
  const levelHeaders = Array.from({ length: levelCount }, (_, index) => {
    const classes = ["section-overview-path-header"];
    if (index === 0) {
      classes.push("is-sticky-l0");
    }
    if (index === 1) {
      classes.push("is-sticky-l1");
    }
    return `<th class="${classes.join(" ")}">L${index}</th>`;
  }).join("");
  const trailing = (trailingHeaders || []).map((label) => `<th>${escapeHtml(label)}</th>`).join("");
  head.innerHTML = `<tr><th class="section-overview-scope-header is-sticky-scope">附录域</th>${levelHeaders}${trailing}</tr>`;
}

function renderSectionOverviewPathCard(node, titleMeta, isCurrent) {
  const toneClasses = [node.is_root ? "is-root" : "", isCurrent ? "is-current" : "is-ancestor"].filter(Boolean).join(" ");
  return `<div class="section-overview-path-card ${toneClasses}">
    <div class="section-overview-path-label">${escapeHtml(node.label || "-")}</div>
    ${titleMeta ? `<div class="section-overview-path-meta">${escapeHtml(titleMeta)}</div>` : ""}
  </div>`;
}

function renderSectionOverviewPathCells(row, levelCount) {
  const path = Array.isArray(row.level_path) ? row.level_path : [];
  return Array.from({ length: levelCount }, (_, levelIndex) => {
    const node = path[levelIndex];
    const classes = ["section-overview-path-cell"];
    if (levelIndex === 0) {
      classes.push("is-sticky-l0");
    }
    if (levelIndex === 1) {
      classes.push("is-sticky-l1");
    }
    if (!node) {
      classes.push("is-empty");
      return `<td class="${classes.join(" ")}"><span class="section-overview-empty-slot"></span></td>`;
    }
    const isCurrent = levelIndex === path.length - 1;
    classes.push(isCurrent ? "is-current" : "is-ancestor");
    return `<td class="${classes.join(" ")}">${renderSectionOverviewPathCard(
      node,
      isCurrent ? row.title_meta : "",
      isCurrent
    )}</td>`;
  }).join("");
}

function buildSectionOverviewRows(rows) {
  const sourceRows = (rows || []).filter(
    (row) => (row?.section_code || row?.section_title) && !row?.is_appendix_scope_root
  );
  const rowByKey = new Map();
  const rowByScopeCode = new Map();

  sourceRows.forEach((row) => {
    const sectionKey = String(row.section_instance_key || row.section_key || "").trim();
    const scopeCodeKey = buildSectionOverviewLookupKey(row.appendix_scope, row.section_code);
    if (sectionKey) {
      rowByKey.set(sectionKey, row);
    }
    if (scopeCodeKey && !rowByScopeCode.has(scopeCodeKey)) {
      rowByScopeCode.set(scopeCodeKey, row);
    }
  });

  const depthCache = new Map();
  const visiting = new Set();
  const pathCache = new Map();
  const pathVisiting = new Set();

  function findParentRow(row) {
    const parentKey = String(row.structural_parent_section_key || row.parent_section_key || "").trim();
    const parentCode = String(row.structural_parent_section_code || row.parent_section_code || "").trim();
    const lookupKey = buildSectionOverviewLookupKey(row.appendix_scope, parentCode);
    return (parentKey ? rowByKey.get(parentKey) : null) || (lookupKey ? rowByScopeCode.get(lookupKey) : null) || null;
  }

  function resolveDepth(row) {
    const sectionKey = String(row.section_instance_key || row.section_key || "").trim();
    if (sectionKey && depthCache.has(sectionKey)) {
      return depthCache.get(sectionKey);
    }
    if (sectionKey && visiting.has(sectionKey)) {
      return Math.max(0, Number(row.section_level || 1) - 1);
    }
    if (sectionKey) {
      visiting.add(sectionKey);
    }
    const parentRow = findParentRow(row);
    const depth = parentRow ? resolveDepth(parentRow) + 1 : 0;
    if (sectionKey) {
      depthCache.set(sectionKey, depth);
      visiting.delete(sectionKey);
    }
    return depth;
  }

  function resolvePath(row) {
    const sectionKey = String(row.section_instance_key || row.section_key || "").trim();
    if (sectionKey && pathCache.has(sectionKey)) {
      return pathCache.get(sectionKey);
    }
    if (sectionKey && pathVisiting.has(sectionKey)) {
      return [row];
    }
    if (sectionKey) {
      pathVisiting.add(sectionKey);
    }
    const parentRow = findParentRow(row);
    const path = parentRow ? [...resolvePath(parentRow), row] : [row];
    if (sectionKey) {
      pathCache.set(sectionKey, path);
      pathVisiting.delete(sectionKey);
    }
    return path;
  }

  return sourceRows.map((row, index) => ({
    appendix_scope: row.appendix_scope || "",
    appendix_title: row.appendix_title || "",
    section_code: row.section_code || "",
    section_title: row.section_title || "",
    numbering_level: Number(row.numbering_level ?? row.section_level ?? 0),
    structural_level: Number(row.structural_level ?? resolveDepth(row)),
    tree_depth: resolveDepth(row),
    node_label: buildSectionOverviewNodeLabel(row),
    parent_label: buildSectionOverviewParentLabel(row),
    is_appendix_scope_root: Boolean(row.is_appendix_scope_root),
    parent_section_code: row.parent_section_code || "",
    source_page_start: row.source_page_start ?? null,
    source_page_end: row.source_page_end ?? null,
    requirement_count: row.requirement_count ?? 0,
    total_chars: row.total_chars ?? row.section_char_count ?? row.char_count ?? 0,
    requirement_like: row.requirement_like,
    title_meta: buildSectionOverviewTitleMeta({ ...row, node_label: buildSectionOverviewNodeLabel(row) }),
    level_path: resolvePath(row)
      .filter((pathRow) => !pathRow?.is_appendix_scope_root)
      .map((pathRow) => ({
      label: buildSectionOverviewNodeLabel(pathRow),
      is_root: Boolean(pathRow.is_appendix_scope_root),
      })),
    outline_order: Number.isFinite(Number(row.tree_order))
      ? Number(row.tree_order)
      : Number.isFinite(Number(row.outline_order))
      ? Number(row.outline_order)
      : index,
    section_order: index,
  }));
}

function renderSectionOverviewTable(rows) {
  const levelCount = Math.max(
    1,
    ...rows.map((row) => (Array.isArray(row.level_path) ? row.level_path.length : 0))
  );
  renderSectionOverviewHead(el.sectionOverviewBody, levelCount, ["页范围", "字符数", "需求候选"]);
  if (!rows.length) {
    el.sectionOverviewBody.innerHTML = `<tr><td colspan="${levelCount + 4}">未识别到章节</td></tr>`;
    return;
  }

  el.sectionOverviewBody.innerHTML = rows
    .map((row) => {
      const span = row.source_page_start ? `${row.source_page_start}-${row.source_page_end ?? row.source_page_start}` : "-";
      const scopeTitle = row.appendix_title ? ` title="${escapeHtml(row.appendix_title)}"` : "";
      return `<tr>
        <td class="section-overview-scope-cell is-sticky-scope"><span class="section-overview-scope-pill"${scopeTitle}>${escapeHtml(formatScope(row.appendix_scope, row.appendix_title))}</span></td>
        ${renderSectionOverviewPathCells(row, levelCount)}
        <td class="section-overview-metric-cell"><span class="section-overview-page-pill">${escapeHtml(span)}</span></td>
        <td class="section-overview-metric-cell"><span class="section-overview-page-pill">${escapeHtml(String(row.total_chars ?? 0))}</span></td>
        <td class="section-overview-metric-cell"><span class="section-overview-flag ${row.requirement_like ? "is-yes" : "is-no"}">${row.requirement_like ? "是" : "否"}</span></td>
      </tr>`;
    })
    .join("");
}

function compareSectionOverviewRows(a, b) {
  if (a.outline_order !== b.outline_order) {
    return a.outline_order - b.outline_order;
  }
  const scopeCmp = compareScopeCode(a.appendix_scope, b.appendix_scope);
  if (scopeCmp !== 0) {
    return scopeCmp;
  }
  if (a.section_order !== b.section_order) {
    return a.section_order - b.section_order;
  }
  if ((a.req_order ?? Number.MAX_SAFE_INTEGER) !== (b.req_order ?? Number.MAX_SAFE_INTEGER)) {
    return (a.req_order ?? Number.MAX_SAFE_INTEGER) - (b.req_order ?? Number.MAX_SAFE_INTEGER);
  }
  return naturalCompareSectionCode(a.section_code, b.section_code);
}

function compareScopeCode(a, b) {
  const left = String(a || "MAIN").trim().toUpperCase();
  const right = String(b || "MAIN").trim().toUpperCase();
  if (left === right) {
    return 0;
  }
  if (left === "MAIN") {
    return -1;
  }
  if (right === "MAIN") {
    return 1;
  }
  const leftMatch = left.match(/^SUB(\d+)$/);
  const rightMatch = right.match(/^SUB(\d+)$/);
  if (leftMatch && rightMatch) {
    return Number(leftMatch[1]) - Number(rightMatch[1]);
  }
  if (leftMatch) {
    return -1;
  }
  if (rightMatch) {
    return 1;
  }
  return left.localeCompare(right);
}

function naturalCompareSectionCode(a, b) {
  return String(a || "").localeCompare(String(b || ""), "zh-CN", {
    numeric: true,
    sensitivity: "base",
  });
}

function normalizeSectionTitle(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase();
}

function formatConsistencyIssueType(issueType) {
  const normalized = String(issueType || "").trim();
  if (!normalized) {
    return "";
  }
  if (normalized === "missing_scope_root") {
    return "附件根节点缺失";
  }
  if (normalized === "source_faithful_duplicate_code") {
    return "源文档重复编号";
  }
  if (normalized === "duplicate_code") {
    return "重复编号";
  }
  if (normalized === "missing_sibling_code") {
    return "缺失同级编号";
  }
  if (normalized === "level_jump") {
    return "层级跳变";
  }
  if (normalized === "late_major_reset") {
    return "晚期主编号重置";
  }
  return normalized;
}

function renderConsistencyTable(consistency) {
  const rows = consistency?.issues || [];
  if (!rows.length) {
    el.consistencyBody.innerHTML = `<tr><td colspan="5">未发现章节一致性问题</td></tr>`;
    return;
  }
  el.consistencyBody.innerHTML = rows
    .map((row) => {
      return `<tr>
        <td>${escapeHtml(formatConsistencyIssueType(row.issue_type))}</td>
        <td>${escapeHtml(row.severity || "")}</td>
        <td>${escapeHtml(formatScope(row.appendix_scope, row.appendix_title))}</td>
        <td>${escapeHtml(row.section_code || "")}</td>
        <td>${escapeHtml(row.detail || "")}</td>
      </tr>`;
    })
    .join("");
}

function renderTypeFilter(requirements) {
  const kinds = new Set(
    (requirements || [])
      .map((item) => String(item.entry_kind || "REQUIREMENT").toUpperCase())
      .filter(Boolean)
  );
  const orderedKinds = ["HEADING", "HYBRID", "REQUIREMENT"].filter((kind) => kinds.has(kind));
  if (!el.typeFilterGroup) {
    return;
  }
  el.typeFilterGroup.innerHTML = orderedKinds
    .map((kind) => {
      const active = state.selectedKinds.has(kind) ? "is-active" : "";
      return `<button type="button" class="type-filter-chip ${active}" data-kind-chip="${kind}">${escapeHtml(
        kind === "HEADING" ? "HEADING 章节" : kind === "HYBRID" ? "HYBRID 合并" : "REQUIREMENT 需求"
      )}</button>`;
    })
    .join("");
  el.typeFilterGroup.querySelectorAll("[data-kind-chip]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = String(button.getAttribute("data-kind-chip") || "").trim().toUpperCase();
      if (!kind) {
        return;
      }
      if (state.selectedKinds.has(kind)) {
        state.selectedKinds.delete(kind);
      } else {
        state.selectedKinds.add(kind);
      }
      renderTypeFilter(getReviewEntries());
      renderFilteredRequirements();
    });
  });
}

function renderFilteredRequirements() {
  const rows = getReviewEntries();
  const sectionFilter = (el.sectionFilter.value || "").trim().toLowerCase();
  const keyword = (el.keywordFilter.value || "").trim().toLowerCase();

  const filtered = filterReviewEntries(rows, { sectionFilter, keyword }).filter((row, index) => {
    const selectionKey = getSelectionKey(row, index);
    if (state.showSelectedOnly) {
      if (!state.selectedReq[selectionKey]) {
        return false;
      }
    }
    if (state.showRecommendedSourceOnly && !rowHasRecommendedSource(row)) {
      return false;
    }
    return true;
  });

  if (!filtered.length) {
    el.requirementBody.innerHTML = `<tr><td colspan="6">没有匹配条目</td></tr>`;
    updateSelectionStatus([], rows);
    return;
  }

  el.requirementBody.innerHTML = filtered
    .map((row, index) => {
      const span = `${row.source_page ?? ""}-${row.source_page_end ?? ""}`;
      const entryId = String(row.entry_id || row.req_code || `entry-${index}`);
      const entryKind = String(row.entry_kind || "REQUIREMENT").toUpperCase();
      const isHeading = entryKind === "HEADING";
      const selectionKey = getSelectionKey(row, index);
      const checked = state.selectedReq[selectionKey] ? "checked" : "";
      const pageStart = Number(row.source_page || 0);
      const pageAttr = pageStart > 0 ? String(pageStart) : "";
      const activeClass = entryId && entryId === state.activeEntryId ? "is-active" : "";
      const headingClass = isHeading ? "is-heading" : "";
      const textValue = getRowDisplayText(row);
      const canToggleRaw = canToggleRawText(row);
      const toggleLabel = isRowShowingRaw(entryId, row) ? "查看增量" : "查看原文";
      const summary = renderTableDecisionSummary(row);
      return `<tr class="${activeClass} ${headingClass}" data-entry-id="${escapeHtml(
        entryId
      )}" data-pdf-page="${escapeHtml(pageAttr)}">
        <td><input type="checkbox" data-select-key="${escapeHtml(selectionKey)}" ${checked} /></td>
        <td>
          <select class="kind-select" data-kind-select="1" data-entry-id="${escapeHtml(entryId)}">
            <option value="HEADING" ${entryKind === "HEADING" ? "selected" : ""}>HEADING</option>
            <option value="REQUIREMENT" ${entryKind === "REQUIREMENT" ? "selected" : ""}>REQUIREMENT</option>
            <option value="HYBRID" ${entryKind === "HYBRID" ? "selected" : ""}>HYBRID</option>
          </select>
        </td>
        <td>${escapeHtml(formatScope(row.appendix_scope))}</td>
        <td>${escapeHtml(row.section_code || "")}</td>
        <td class="req-text">${
          canToggleRaw
            ? `<div class="req-text-toolbar"><button type="button" class="req-text-switch" data-toggle-raw="${escapeHtml(
                entryId
              )}">${toggleLabel}</button></div>`
            : ""
        }${summary}${renderRequirementContent({
          ...row,
          raw_text: textValue,
          display_text: textValue,
          requirement_text: textValue,
        })}</td>
        <td>${escapeHtml(span || "-")}</td>
      </tr>`;
    })
    .join("");
  updateSelectionStatus(filtered, rows);
}

function filterReviewEntries(rows, { sectionFilter = "", keyword = "" } = {}) {
  return (rows || []).filter((row) => {
    const entryKind = String(row.entry_kind || "REQUIREMENT").toUpperCase();
    if (state.selectedKinds.size && !state.selectedKinds.has(entryKind)) {
      return false;
    }
    const sectionKey = String(
      row.section_instance_key || row.section_key || [row.appendix_scope, row.section_code].filter(Boolean).join(":")
    ).toLowerCase();
    if (sectionFilter && !sectionKey.includes(sectionFilter)) {
      return false;
    }
    if (keyword) {
      const searchText = [
        row.req_code || "",
        row.section_title || "",
        row.display_text || "",
        row.raw_text || "",
        row.requirement_text || "",
      ]
        .join("\n")
        .toLowerCase();
      if (!searchText.includes(keyword)) {
        return false;
      }
    }
    return true;
  });
}

function rowHasRecommendedSource(row) {
  return (Array.isArray(row?.table_refs) ? row.table_refs : []).some(
    (ref) => String(ref?.recommended_export_variant || "").trim().toLowerCase() === "source"
  );
}

function isLockedTableRef(ref) {
  return Boolean(ref?.export_variant_locked);
}

function renderTableDecisionSummary(row) {
  const refs = Array.isArray(row?.table_refs) ? row.table_refs : [];
  const summaryChips = [];
  if (Boolean(row?.complex_table_mode)) {
    summaryChips.push(`<span class="table-export-badge complex-badge">COMPLEX TABLE</span>`);
    const role = String(row?.complex_role || "").trim();
    if (role === "zone_summary") {
      summaryChips.push(`<span class="table-export-chip">整表总览</span>`);
    } else if (role === "group") {
      summaryChips.push(`<span class="table-export-chip">${escapeHtml(String(row?.complex_group_label || "分组"))}</span>`);
    }
  }
  if (refs.length) {
    const selectedSource = refs.filter((ref) => {
      return String(ref?.export_variant || "source").trim().toLowerCase() === "source" && ref?.source_snapshot_url;
    }).length;
    summaryChips.push(`<span class="table-export-badge">共 ${refs.length} 个表格</span>`);
    summaryChips.push(`<span class="table-export-chip">当前快照 ${selectedSource}/${refs.length}</span>`);
    if (refs.some((ref) => isLockedTableRef(ref))) {
      summaryChips.push(`<span class="table-export-chip">原文锁定</span>`);
    }
  }
  if (!summaryChips.length) {
    return "";
  }
  return `<div class="table-export-summary">${summaryChips.join("")}</div>`;
}

function updateSelectionStatus(filteredRows, allRows) {
  if (!el.selectionStatus) {
    return;
  }
  const currentRows = filteredRows || [];
  const universeRows = allRows || [];
  const selectableCurrent = currentRows;
  const selectedCurrent = selectableCurrent.filter((row, index) => state.selectedReq[getSelectionKey(row, index)]).length;
  const selectableAll = universeRows;
  const selectedAll = selectableAll.filter((row, index) => state.selectedReq[getSelectionKey(row, index)]).length;
  el.selectionStatus.textContent = `当前保留 ${selectedCurrent}/${selectableCurrent.length}，总保留 ${selectedAll}/${selectableAll.length}`;
}

function buildReviewEntries(preview = state.preview, useRaw = false) {
  const workspaceRows = useRaw ? preview?.review_entries_raw : preview?.review_entries;
  const completeRows =
    workspaceRows ||
    (useRaw
      ? preview?.complete_requirement_entries_raw || preview?.complete_requirement_entries
      : preview?.complete_requirement_entries);
  if (Array.isArray(completeRows) && completeRows.length) {
    return completeRows.map((row, index) => normalizeReviewEntry(row, index));
  }
  const requirements = preview?.requirements || preview?.preview?.requirements || [];
  return requirements.map((row, index) => normalizeReviewEntry(row, index));
}

function getReviewEntries() {
  const current = state.showRawDuplicateRows ? state.reviewEntriesRaw : state.reviewEntries;
  return Array.isArray(current) ? current : [];
}

function normalizeReviewEntry(row, index) {
  const entryKind = String(row?.entry_kind || "REQUIREMENT").toUpperCase();
  const requirementText = String(row?.requirement_text || "").trim();
  const rawText = String(row?.raw_text || requirementText).trim();
  const displayText = String(row?.display_text || requirementText || rawText).trim() || rawText;
  return {
    ...(row || {}),
    entry_id: row?.entry_id || row?.req_code || `ENTRY-${index + 1}`,
    entry_kind: entryKind,
    display_text: displayText,
    raw_text: rawText || displayText,
    requirement_text: displayText,
    text_relation: String(row?.text_relation || "distinct"),
    is_deduped: Boolean(row?.is_deduped),
    keep: typeof row?.keep === "boolean" ? Boolean(row.keep) : null,
  };
}

function complexRowPayload(row) {
  return {
    complex_table_mode: Boolean(row?.complex_table_mode),
    complex_zone_id: row?.complex_zone_id || "",
    complex_family: row?.complex_family || "",
    complex_role: row?.complex_role || "",
    complex_group_label: row?.complex_group_label || "",
    complex_split_level: row?.complex_split_level || "",
    complex_warnings: Array.isArray(row?.complex_warnings) ? row.complex_warnings : [],
    complex_row_policy: row?.complex_row_policy || "",
  };
}

function getRowDisplayText(row) {
  const entryId = String(row?.entry_id || "");
  const showRaw = isRowShowingRaw(entryId, row);
  if (showRaw) {
    return String(row?.raw_text || row?.requirement_text || row?.display_text || row?.section_title || "");
  }
  return String(row?.display_text || row?.requirement_text || row?.section_title || "");
}

function isRowShowingRaw(entryId, row) {
  if (!canToggleRawText(row)) {
    return false;
  }
  if (!entryId) {
    return true;
  }
  if (Object.prototype.hasOwnProperty.call(state.rawViewByEntryId, entryId)) {
    return Boolean(state.rawViewByEntryId[entryId]);
  }
  return true;
}

function canToggleRawText(row) {
  const raw = String(row?.raw_text || "").trim();
  const display = String(row?.display_text || row?.requirement_text || "").trim();
  if (!raw || !display) {
    return false;
  }
  return raw !== display;
}

function getSelectionKey(row, index = 0) {
  const entryId = String(row?.entry_id || "").trim();
  if (entryId) {
    return entryId;
  }
  const reqCode = String(row?.req_code || "").trim();
  if (reqCode) {
    return reqCode;
  }
  return `row-${index}`;
}

function initRequirementSelection(entries) {
  state.selectedReq = {};
  (entries || []).forEach((row, index) => {
    const explicitKeep = typeof row?.keep === "boolean" ? Boolean(row.keep) : null;
    state.selectedReq[getSelectionKey(row, index)] =
      explicitKeep === null ? shouldDefaultKeepEntry(row, entries) : explicitKeep;
  });
  state.showSelectedOnly = false;
  state.activeEntryId = "";
}

function bulkSelect(value) {
  const rows = filterReviewEntries(getReviewEntries(), {
    sectionFilter: (el.sectionFilter.value || "").trim().toLowerCase(),
    keyword: (el.keywordFilter.value || "").trim().toLowerCase(),
  });
  rows.forEach((row, index) => {
    state.selectedReq[getSelectionKey(row, index)] = Boolean(value);
  });
  renderFilteredRequirements();
}

function toggleSelectedOnly() {
  state.showSelectedOnly = !state.showSelectedOnly;
  syncSelectionButtons();
  renderFilteredRequirements();
}

function toggleRecommendedSourceOnly() {
  state.showRecommendedSourceOnly = !state.showRecommendedSourceOnly;
  syncSelectionButtons();
  renderFilteredRequirements();
}

function onRawRowsToggleChanged(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) {
    return;
  }
  state.showRawDuplicateRows = Boolean(target.checked);
  const rows = getReviewEntries();
  initRequirementSelection(rows);
  renderTypeFilter(rows);
  syncSelectionButtons();
  renderFilteredRequirements();
}

async function onApplyReviewedExtraction() {
  if (!state.projectId || !state.documentId) {
    alert("请先选择项目和 SOR 文档。");
    return;
  }
  const dedupedRows = Array.isArray(state.reviewEntries) ? state.reviewEntries : [];
  const payload = {
    entries: dedupedRows.map((row, index) => ({
      entry_id: row.entry_id || "",
      req_code: row.req_code || "",
      entry_kind: String(row.entry_kind || "REQUIREMENT").toUpperCase(),
      source_page: Number(row.source_page || 0) || null,
      source_page_end: Number(row.source_page_end || 0) || null,
      source_anchor: row.source_anchor || "",
      requirement_text: row.requirement_text || "",
      display_text: row.display_text || row.requirement_text || "",
      raw_text: row.raw_text || row.requirement_text || "",
      requirement_type: row.requirement_type || "",
      priority: row.priority || "",
      text_relation: row.text_relation || "distinct",
      is_deduped: Boolean(row.is_deduped),
      heading_text: row.heading_text || "",
      parent_section_code: row.parent_section_code || "",
      parent_section_title: row.parent_section_title || "",
      parent_section_label: row.parent_section_label || "",
      merged_from_req_codes: Array.isArray(row.merged_from_req_codes) ? row.merged_from_req_codes : [],
      merged_from_entry_ids: Array.isArray(row.merged_from_entry_ids) ? row.merged_from_entry_ids : [],
      table_refs: Array.isArray(row.table_refs) ? row.table_refs : [],
      content_blocks: Array.isArray(row.content_blocks) ? row.content_blocks : [],
      has_table_asset: Boolean(row.has_table_asset),
      table_render_mode: row.table_render_mode || "",
      keep: Boolean(state.selectedReq[getSelectionKey(row, index)]),
      dedupe_version: row.dedupe_version || state.preview?.summary?.dedupe_version || "",
      ...complexRowPayload(row),
    })),
  };
  const button = el.applyReviewedBtn;
  if (button) {
    button.disabled = true;
  }
  try {
    const result = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.documentId}/apply-reviewed-extraction`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    const workspace = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.documentId}/workspace?include_preview=1`
    );
    state.workspace = workspace;
    state.preview = workspace.preview || null;
    renderPreview(workspace);
    if (el.statusLine) {
      el.statusLine.textContent = `审阅结果已写入工作区：${result.requirements_generated} 条，条目集版本 ${result.requirement_set_version}。`;
    }
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

async function onLockReviewedExtraction() {
  if (!state.projectId || !state.documentId) {
    alert("请先选择项目和 SOR 文档。");
    return;
  }
  if (el.lockReviewedBtn) {
    el.lockReviewedBtn.disabled = true;
  }
  try {
    const workspace = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.documentId}/lock-extraction`,
      { method: "POST" }
    );
    state.workspace = {
      ...(state.workspace || {}),
      ...workspace,
      preview: state.preview || state.workspace?.preview || null,
      review_entries: state.workspace?.review_entries || [],
      review_entries_raw: state.workspace?.review_entries_raw || [],
    };
    syncWorkspaceStageActions();
    renderSummary(state.workspace);
    if (el.statusLine) {
      el.statusLine.textContent = "条目集已锁定，可以切到主控台运行 AI 分析。";
    }
  } finally {
    syncWorkspaceStageActions();
  }
}

function syncSelectionButtons() {
  if (!el.toggleSelectedBtn) {
    return;
  }
  el.toggleSelectedBtn.textContent = state.showSelectedOnly ? "显示全部" : "仅看保留";
  if (el.toggleSourceRecommendedBtn) {
    el.toggleSourceRecommendedBtn.textContent = state.showRecommendedSourceOnly ? "显示全部条目" : "仅看推荐截图";
  }
}

function syncWorkspaceStageActions() {
  const actions = state.workspace?.actions || {};
  const workflowStage = String(state.workspace?.document?.workflow_stage || "").toUpperCase();
  if (el.applyReviewedBtn) {
    el.applyReviewedBtn.disabled = !state.projectId || !state.documentId;
  }
  if (el.lockReviewedBtn) {
    el.lockReviewedBtn.disabled =
      !state.projectId ||
      !state.documentId ||
      !Boolean(actions.can_lock_extraction) ||
      workflowStage === "EXTRACTION_LOCKED" ||
      workflowStage === "AI_ANALYZING" ||
      workflowStage === "AI_READY";
  }
}

function onRequirementSelectionChanged(event) {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.matches("select[data-kind-select]")) {
    const entryId = String(target.getAttribute("data-entry-id") || "");
    const nextKind = String(target.value || "REQUIREMENT").toUpperCase();
    const row = getReviewEntries().find((item) => String(item.entry_id || "") === entryId);
    if (!row) {
      return;
    }
    row.entry_kind = nextKind === "HEADING" ? "HEADING" : nextKind === "HYBRID" ? "HYBRID" : "REQUIREMENT";
    syncEntryKindAcrossViews(entryId, row.entry_kind);
    const entryIndex = getReviewEntries().findIndex((item) => String(item.entry_id || "") === entryId);
    const selectionKey = getSelectionKey(row, entryIndex);
    if (!(selectionKey in state.selectedReq)) {
      state.selectedReq[selectionKey] = shouldDefaultKeepEntry(row, getReviewEntries());
    }
    renderTypeFilter(getReviewEntries());
    renderFilteredRequirements();
    return;
  }
  if (!(target instanceof HTMLInputElement)) {
    return;
  }
  if (target.type !== "checkbox") {
    return;
  }
  const selectionKey = target.getAttribute("data-select-key") || "";
  if (!selectionKey) {
    return;
  }
  state.selectedReq[selectionKey] = target.checked;
  if (state.showSelectedOnly && !target.checked) {
    renderFilteredRequirements();
  }
}

function syncEntryKindAcrossViews(entryId, nextKind) {
  [state.reviewEntries, state.reviewEntriesRaw].forEach((rows) => {
    (rows || []).forEach((item) => {
      if (String(item.entry_id || "") === String(entryId || "")) {
        item.entry_kind = nextKind;
      }
    });
  });
}

async function onRequirementTableClicked(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (await handleTableViewToggle(target)) {
    event.preventDefault();
    return;
  }
  const toggleBtn = target.closest("button[data-toggle-raw]");
  if (toggleBtn instanceof HTMLButtonElement) {
    const entryId = String(toggleBtn.getAttribute("data-toggle-raw") || "").trim();
    if (entryId) {
      toggleEntryRawView(entryId);
    }
    return;
  }
  if (target.closest('input[type="checkbox"]') || target.closest("select[data-kind-select]")) {
    return;
  }

  const row = target.closest("tr[data-pdf-page]");
  if (!(row instanceof HTMLTableRowElement)) {
    return;
  }
  const entryId = row.getAttribute("data-entry-id") || "";
  const pageNo = Number(row.getAttribute("data-pdf-page") || 0);
  focusRequirementPdf(entryId, pageNo);
}

async function handleTableViewToggle(target) {
  const button = target.closest("button[data-table-variant]");
  if (!(button instanceof HTMLButtonElement)) {
    return false;
  }
  const tableId = String(button.getAttribute("data-table-id") || "").trim();
  const variant = String(button.getAttribute("data-table-variant") || "").trim();
  if (!tableId) {
    return true;
  }
  const currentRef = findTableRefById(tableId);
  if (isLockedTableRef(currentRef)) {
    return true;
  }
  const nextVariant = variant === "slice" ? "slice" : "source";
  const affected = await setTableExportVariantAcrossViews(tableId, nextVariant);
  state.tableViewByTableId[tableId] = nextVariant;

  if (affected && affected.updated_requirement) {
    const ref = (Array.isArray(affected.updated_requirement.table_refs) ? affected.updated_requirement.table_refs : []).find(r => String(r.table_id) === tableId);
    if (ref) {
      document.querySelectorAll(`div.table-asset-switcher[data-table-id="${tableId}"]`).forEach(el => {
        el.outerHTML = renderTableRef(ref);
      });
      document.querySelectorAll(`div.table-export-summary-container[data-req-id="${affected.updated_requirement.id}"]`).forEach(el => {
        el.innerHTML = renderTableDecisionSummary(affected.updated_requirement);
      });
    }
  }
  if (!affected) {
    return true;
  }
  try {
    await saveReviewEntry(affected);
  } catch (error) {
    restoreTableExportVariantAcrossViews(tableId, affected.previousVariant);
    renderFilteredRequirements();
    if (el.statusLine) {
      el.statusLine.textContent = normalizeApiError(error, "导出表格选择保存失败，请稍后重试。");
    }
  }
  return true;
}

function findTableRefById(tableId) {
  for (const rows of [state.reviewEntries, state.reviewEntriesRaw]) {
    for (const row of rows || []) {
      const ref = (Array.isArray(row?.table_refs) ? row.table_refs : []).find(
        (item) => String(item?.table_id || "").trim() === String(tableId || "").trim()
      );
      if (ref) {
        return ref;
      }
    }
  }
  return null;
}

function setTableExportVariantAcrossViews(tableId, nextVariant) {
  const normalized = nextVariant === "source" ? "source" : "slice";
  let affected = null;
  [state.reviewEntries, state.reviewEntriesRaw].forEach((rows) => {
    (rows || []).forEach((row) => {
      let touched = false;
      (Array.isArray(row?.table_refs) ? row.table_refs : []).forEach((ref) => {
        if (String(ref?.table_id || "").trim() !== tableId) {
          return;
        }
        if (!affected) {
          affected = {
            entryId: String(row.entry_id || ""),
            previousVariant: String(ref.export_variant || "").trim().toLowerCase() || "slice",
            row,
          };
        }
        if (isLockedTableRef(ref)) {
          return;
        }
        ref.export_variant = normalized;
        ref.export_variant_overridden =
          normalized !== String(ref.recommended_export_variant || "slice").trim().toLowerCase();
        touched = true;
      });
      if (touched) {
        row.has_table_asset = Boolean(row.has_table_asset);
      }
    });
  });
  return affected?.row || null;
}

function restoreTableExportVariantAcrossViews(tableId, previousVariant) {
  const normalized = previousVariant === "source" ? "source" : "slice";
  [state.reviewEntries, state.reviewEntriesRaw].forEach((rows) => {
    (rows || []).forEach((row) => {
      (Array.isArray(row?.table_refs) ? row.table_refs : []).forEach((ref) => {
        if (String(ref?.table_id || "").trim() !== tableId) {
          return;
        }
        if (isLockedTableRef(ref)) {
          return;
        }
        ref.export_variant = normalized;
        ref.export_variant_overridden =
          normalized !== String(ref.recommended_export_variant || "slice").trim().toLowerCase();
      });
    });
  });
  state.tableViewByTableId[tableId] = normalized;
}

async function saveReviewEntry(row) {
  if (!state.projectId || !state.documentId) {
    return;
  }
  const result = await api(`/api/projects/${state.projectId}/sor-documents/${state.documentId}/review-entry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      entry_id: row.entry_id || "",
      req_code: row.req_code || "",
      entry_kind: row.entry_kind || "REQUIREMENT",
      source_page: Number(row.source_page || 0) || null,
      source_page_end: Number(row.source_page_end || 0) || null,
      source_anchor: row.source_anchor || "",
      requirement_text: row.raw_text || row.requirement_text || row.display_text || "",
      display_text: row.display_text || row.requirement_text || row.raw_text || "",
      raw_text: row.raw_text || row.requirement_text || row.display_text || "",
      requirement_type: row.requirement_type || "FUNCTIONAL",
      priority: row.priority || "MEDIUM",
      text_relation: row.text_relation || "distinct",
      is_deduped: Boolean(row.is_deduped),
      heading_text: row.heading_text || "",
      parent_section_code: row.parent_section_code || "",
      parent_section_title: row.parent_section_title || "",
      parent_section_label: row.parent_section_label || "",
      merged_from_req_codes: Array.isArray(row.merged_from_req_codes) ? row.merged_from_req_codes : [],
      merged_from_entry_ids: Array.isArray(row.merged_from_entry_ids) ? row.merged_from_entry_ids : [],
      table_refs: Array.isArray(row.table_refs) ? row.table_refs : [],
      content_blocks: Array.isArray(row.content_blocks) ? row.content_blocks : [],
      has_table_asset: Boolean(row.has_table_asset),
      table_render_mode: row.table_render_mode || "",
      dedupe_version: row.dedupe_version || "",
      keep: Boolean(row.keep ?? true),
      ...complexRowPayload(row),
    }),
  });
  state.workspace = {
    ...(state.workspace || {}),
    project: result.project || state.workspace?.project,
    document: result.document || state.workspace?.document,
    summary: result.summary || state.workspace?.summary,
    actions: result.actions || state.workspace?.actions,
  };
  mergeSavedReviewEntry(result.saved_entry || null);
  if (el.statusLine) {
    el.statusLine.textContent = "导出表格选择已保存。";
  }
}

function mergeSavedReviewEntry(savedEntry) {
  if (!savedEntry || typeof savedEntry !== "object") {
    return;
  }
  const key = String(savedEntry.entry_id || savedEntry.req_code || "").trim();
  [state.reviewEntries, state.reviewEntriesRaw].forEach((rows) => {
    const index = (rows || []).findIndex((item) => String(item?.entry_id || item?.req_code || "").trim() === key);
    if (index >= 0) {
      rows[index] = {
        ...rows[index],
        ...savedEntry,
      };
    }
  });
}

function toggleEntryRawView(entryId) {
  const key = String(entryId || "").trim();
  if (!key) {
    return;
  }
  const row =
    getReviewEntries().find((item) => String(item.entry_id || "") === key) ||
    state.reviewEntriesRaw.find((item) => String(item.entry_id || "") === key);
  state.rawViewByEntryId[key] = !isRowShowingRaw(key, row);
  renderFilteredRequirements();
}

function shouldDefaultKeepEntry(row, entries) {
  const kind = String(row?.entry_kind || "REQUIREMENT").toUpperCase();
  if (kind === "HEADING") {
    return !isHeadingRepresentedByContent(row, entries);
  }
  return true;
}

function isHeadingRepresentedByContent(headingRow, entries) {
  const sectionKey = getEntrySectionKey(headingRow);
  const headingText = normalizeComparableText(
    String(
      headingRow?.heading_text || headingRow?.section_title || headingRow?.raw_text || headingRow?.display_text || ""
    )
  );
  if (!sectionKey || !headingText) {
    return false;
  }
  return (entries || []).some((candidate) => {
    if (candidate === headingRow) {
      return false;
    }
    if (getEntrySectionKey(candidate) !== sectionKey) {
      return false;
    }
    const candidateKind = String(candidate?.entry_kind || "REQUIREMENT").toUpperCase();
    if (candidateKind === "HEADING") {
      return false;
    }
    if (candidateKind === "HYBRID") {
      return true;
    }
    const relation = String(candidate?.text_relation || "").trim().toLowerCase();
    if (relation === "heading_prefix" || relation === "exact_match") {
      return true;
    }
    return [candidate?.raw_text, candidate?.display_text, candidate?.requirement_text].some((text) => {
      const normalized = normalizeComparableText(String(text || ""));
      return Boolean(normalized) && (normalized === headingText || normalized.startsWith(headingText));
    });
  });
}

function getEntrySectionKey(row) {
  return String(
    row?.section_instance_key || row?.section_key || [row?.appendix_scope, row?.section_code].filter(Boolean).join(":")
  ).trim();
}

function normalizeComparableText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[：:]/g, ":")
    .replace(/[（(]/g, "(")
    .replace(/[）)]/g, ")")
    .trim();
}

function focusRequirementPdf(entryId, pageNo) {
  if (!state.pdfBaseUrl) {
    return;
  }
  state.activeEntryId = String(entryId || "");
  const row = getReviewEntries().find((item) => String(item.entry_id || "") === state.activeEntryId);
  const highlightText = buildHighlightText(row);
  if (pageNo > 0) {
    setPdfDrawerOpen(true);
    loadPdfPage(pageNo, false, highlightText);
  }
  renderFilteredRequirements();
}

function buildHighlightText(row) {
  if (!row) {
    return "";
  }
  const text = String(row.raw_text || row.requirement_text || row.section_title || "").trim();
  if (!text) {
    return "";
  }
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => Boolean(line));
  const nonTableLines = lines
    .filter((line) => !line.startsWith("|"))
    .filter((line) => !/^[-*•\[\]√\s]+$/.test(line))
    .map((line) => line.replace(/\s+/g, " ").trim());
  if (nonTableLines.length) {
    const first = nonTableLines.find((line) => line.length >= 8) || nonTableLines[0];
    const second = nonTableLines.find((line) => line !== first && line.length >= 8) || "";
    const merged = [first, second]
      .filter(Boolean)
      .join(" ")
      .slice(0, 220);
    return merged;
  }
  const tableLines = lines
    .filter((line) => line.startsWith("|"))
    .map((line) => parseMarkdownCells(line))
    .filter((cells) => cells.length >= 2)
    .map((cells) => cells.slice(0, 3).join(" "))
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter((line) => line && !/^[-: ]+$/.test(line));
  if (!tableLines.length) {
    return "";
  }
  return tableLines[0].slice(0, 180);
}

function renderRichRequirementText(text) {
  const source = String(text || "").replaceAll("\r\n", "\n");
  if (!source.trim()) {
    return "";
  }
  const lines = source.split("\n");
  if (!lines.some((line) => looksLikeMarkdownTableRow(line)) && looksLikeLowConfidenceTable(lines)) {
    const fallbackTable = renderLowConfidenceTable(lines);
    if (fallbackTable) {
      return `<div class="md-table-warning">低置信度表格恢复，请对照右侧 PDF 复核。</div>${fallbackTable}`;
    }
  }
  const blocks = [];
  const paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) {
      return;
    }
    const merged = paragraph.join("\n").trim();
    paragraph.length = 0;
    if (!merged) {
      return;
    }
    blocks.push(`<div class="md-text-block">${escapeHtml(merged).replaceAll("\n", "<br>")}</div>`);
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (looksLikeMarkdownTableRow(line)) {
      flushParagraph();
      const tableLines = [];
      while (i < lines.length) {
        const current = lines[i];
        if (looksLikeMarkdownTableRow(current)) {
          tableLines.push(current);
          i += 1;
          continue;
        }
        break;
      }
      i -= 1;
      const chunks = splitMarkdownTableChunks(tableLines);
      chunks.forEach((chunk) => {
        const html = renderMarkdownTable(chunk);
        if (html) {
          blocks.push(html);
        }
      });
      continue;
    }
    paragraph.push(line);
  }
  flushParagraph();
  return blocks.join("");
}

function renderRequirementContent(row) {
  const blocks = Array.isArray(row?.content_blocks) ? row.content_blocks : [];
  if (!blocks.length) {
    return renderRichRequirementText(row?.raw_text || row?.display_text || row?.requirement_text || "");
  }
  const tableRefs = new Map(
    (Array.isArray(row?.table_refs) ? row.table_refs : [])
      .filter((item) => item && item.table_id)
      .map((item) => [String(item.table_id), item])
  );
  const rendered = blocks
    .map((block) => {
      const kind = String(block?.kind || "").toLowerCase();
      if (kind === "paragraph") {
        return renderRichRequirementText(block?.text || "");
      }
      if (kind !== "table") {
        return "";
      }
      return renderTableRef(tableRefs.get(String(block?.table_id || "")), row);
    })
    .filter(Boolean);
  return rendered.join("") || renderRichRequirementText(row?.raw_text || row?.display_text || row?.requirement_text || "");
}

function renderTableRef(ref, row = null) {
  if (!ref) {
    return "";
  }
  const tableId = String(ref.table_id || "").trim();
  const variant = activeTableVariant(ref);
  const headers = Array.isArray(ref.inherited_headers) ? ref.inherited_headers : [];
  const rows = Array.isArray(ref.slice_rows) ? ref.slice_rows.filter((row) => Array.isArray(row)) : [];
  const sliceLabel =
    String(ref.slice_render_mode || "") === "structured" && headers.length ? "结构化切片" : "切片快照";
  let content = "";
  if (variant === "source" && ref.source_snapshot_url) {
    content = renderTableSnapshot(ref, row, ref.source_snapshot_url, "source table snapshot");
  } else if (String(ref.slice_render_mode || "") === "structured" && headers.length) {
    content = renderStructuredTable(headers, rows);
  } else if (ref.slice_snapshot_url) {
    content = renderTableSnapshot(ref, row, ref.slice_snapshot_url, "table snapshot");
  } else {
    content = `<div class="md-text-block">${escapeHtml(String(ref.slice_ai_projection || "")).replaceAll("\n", "<br>")}</div>`;
  }
  if (!ref.source_snapshot_url || !tableId) {
    return `${content}`;
  }
  const locked = isLockedTableRef(ref);
  return `
    <div class="table-asset-switcher" data-table-id="${escapeHtml(tableId)}">
      <div class="table-view-toolbar">
        <span class="table-view-label">查看内容</span>
        ${locked ? '<span class="table-view-lock">原文锁定</span>' : ""}
        <button type="button" class="table-view-btn${variant === "slice" ? " is-active" : ""}" data-table-id="${escapeHtml(
          tableId
        )}" data-table-variant="slice"${locked ? " disabled" : ""}>表格</button>
        <button type="button" class="table-view-btn${variant === "source" ? " is-active" : ""}" data-table-id="${escapeHtml(
          tableId
        )}" data-table-variant="source"${locked ? " disabled" : ""}>原文截图</button>
      </div>
      ${content}
    </div>
  `;
}

function activeTableVariant(ref) {
  const tableId = String(ref?.table_id || "").trim();
  if (isLockedTableRef(ref) && ref?.source_snapshot_url) {
    return "source";
  }
  if (!tableId) {
    return ref?.source_snapshot_url ? "source" : "slice";
  }
  const preferred = state.tableViewByTableId[tableId];
  if (preferred === "source" || preferred === "slice") {
    if (preferred === "source" && !ref?.source_snapshot_url) {
      return "slice";
    }
    return preferred;
  }
  const dbVariant = String(ref?.export_variant || "").trim().toLowerCase();
  if (dbVariant === "source" || dbVariant === "slice") {
    if (dbVariant === "source" && !ref?.source_snapshot_url) {
      return "slice";
    }
    return dbVariant;
  }
  return ref?.source_snapshot_url ? "source" : "slice";
}

function isComplexFullPageSpanSnapshot(ref, row) {
  const refMode = String(ref?.complex_snapshot_mode || "").trim().toLowerCase();
  const rowMode = String(row?.complex_snapshot_mode || "").trim().toLowerCase();
  return Boolean(ref?.complex_table_mode || row?.complex_table_mode) && (refMode === "full_page_span" || rowMode === "full_page_span");
}

function renderTableSnapshot(ref, row, url, altText) {
  const fullSpan = isComplexFullPageSpanSnapshot(ref, row);
  const wrapClass = `asset-snapshot-wrap${fullSpan ? " asset-snapshot-wrap-complex-full-span" : ""}`;
  const imageClass = `asset-snapshot${fullSpan ? " asset-snapshot-complex-full-span" : ""}`;
  return `<div class="${wrapClass}"><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"><img class="${imageClass}" src="${escapeHtml(
    url
  )}" alt="${escapeHtml(altText)}" loading="lazy" /></a></div>`;
}

function renderStructuredTable(headers, rows) {
  const colCount = Math.max(
    Array.isArray(headers) ? headers.length : 0,
    ...rows.map((row) => (Array.isArray(row) ? row.length : 0)),
    0
  );
  if (!colCount) {
    return "";
  }
  const normalizedHeaders = Array.from({ length: colCount }, (_, idx) => String((headers && headers[idx]) || "").trim());
  const normalizedRows = rows.map((row) =>
    Array.from({ length: colCount }, (_, idx) => String((row && row[idx]) || "").trim())
  );
  const colPercents = computeMarkdownTableColumnPercents(normalizedHeaders, normalizedRows, colCount);
  const colgroup = colPercents.length
    ? `<colgroup>${colPercents.map((pct) => `<col style="width:${pct.toFixed(2)}%">`).join("")}</colgroup>`
    : "";
  const head = normalizedHeaders.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("");
  const body = normalizedRows
    .map(
      (row) =>
        `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell || ""))}</td>`).join("")}</tr>`
    )
    .join("");
  return `<div class="md-table-wrap asset-table-wrap"><table class="md-table asset-table md-table-accurate">${colgroup}<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function looksLikeMarkdownTableRow(line) {
  const text = String(line || "").trim();
  if (!text || !text.includes("|")) {
    return false;
  }
  const cells = parseMarkdownCells(text);
  return cells.length >= 2;
}

function looksLikeTableBridgeLine(line, nextLine) {
  const text = String(line || "").trim();
  if (!text || text.includes("|")) {
    return false;
  }
  if (!looksLikeMarkdownTableRow(nextLine)) {
    return false;
  }
  if (text.length > 260) {
    return false;
  }
  if (
    /^\d+(?:\.\d+){1,6}\b/.test(text) &&
    !/^\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|h|m\/s)\b/i.test(text)
  ) {
    return false;
  }
  if (/^\d+[)）]\s*.+(?:布置|要求|说明)$/.test(text)) {
    return false;
  }
  if (/^(?:前|后)(?:减振器|蓄能器)$/.test(text)) {
    return false;
  }
  if (/^备注[:：]/.test(text)) {
    return false;
  }
  return true;
}

function parseMarkdownCells(line) {
  let text = String(line || "");
  if (!text.trim()) {
    return [];
  }
  text = text.trim();
  const hasLeadingBar = text.startsWith("|");
  const hasTrailingBar = text.endsWith("|");
  if (hasLeadingBar) {
    text = text.slice(1);
  }
  if (hasTrailingBar) {
    text = text.slice(0, -1);
  }
  const placeholder = "__ESCAPED_PIPE__";
  const escaped = text.replaceAll("\\|", placeholder);
  const cells = escaped.split("|").map((cell) => cell.replaceAll(placeholder, "|").trim());
  if (!cells.length) {
    return [];
  }
  if (cells.length === 1 && !cells[0]) {
    return [];
  }
  return cells;
}

function isMarkdownSeparatorRow(cells) {
  if (!cells.length) {
    return false;
  }
  return cells.every((cell) => /^:?-{2,}:?$/.test(cell));
}

function renderMarkdownTable(lines) {
  const rows = lines
    .map((line) => parseMarkdownCells(line))
    .filter((cells) => cells.length > 0);
  if (!rows.length) {
    return "";
  }
  const maxCols = Math.max(...rows.map((cells) => cells.length));
  const normalized = rows.map((cells) => {
    const row = [...cells];
    while (row.length < maxCols) {
      row.push("");
    }
    return row;
  });

  let head = [];
  let body = [];
  if (normalized.length >= 2 && isMarkdownSeparatorRow(normalized[1])) {
    head = normalized[0];
    body = normalized.slice(2);
  } else {
    head = normalized[0];
    body = normalized.slice(1);
  }
  if (!body.length) {
    body = [head];
    head = [];
  }

  const mergeColumns = canApplyMergeColumns(head, body, maxCols) ? detectMergeColumns(head) : [];
  const mergedBody = mergeColumns.length
    ? mergeBodyCellsByColumn(body, mergeColumns)
    : body.map((cells) => cells.map((cell) => ({ text: cell, rowspan: 1, skip: false })));
  const colPercents = computeMarkdownTableColumnPercents(head, body, maxCols);
  const colgroup = colPercents.length
    ? `<colgroup>${colPercents.map((pct) => `<col style="width:${pct.toFixed(2)}%">`).join("")}</colgroup>`
    : "";

  const thead = head.length
    ? `<thead><tr>${head.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("")}</tr></thead>`
    : "";
  const tbody = `<tbody>${mergedBody
    .map(
      (cells) =>
        `<tr>${cells
          .filter((cell) => !cell.skip)
          .map((cell) =>
            cell.rowspan > 1
              ? `<td rowspan="${cell.rowspan}">${escapeHtml(cell.text)}</td>`
              : `<td>${escapeHtml(cell.text)}</td>`
          )
          .join("")}</tr>`
    )
    .join("")}</tbody>`;
  const table = `<table class="md-table md-table-accurate">${colgroup}${thead}${tbody}</table>`;
  return `<div class="md-table-wrap">${table}</div>`;
}

function computeMarkdownTableColumnPercents(head, body, colCount) {
  const cols = Number(colCount) || 0;
  if (!cols) {
    return [];
  }
  const labels = Array.from({ length: cols }, (_, idx) => String((head && head[idx]) || "").trim());
  const rows = Array.isArray(body) ? body.slice(0, 40) : [];
  const weights = Array.from({ length: cols }, (_, col) => {
    const label = labels[col] || "";
    let maxLen = estimateCellVisualLength(label);
    let sumLen = maxLen;
    let count = label ? 1 : 0;
    rows.forEach((row) => {
      const text = String((row && row[col]) || "").trim();
      if (!text) {
        return;
      }
      const len = estimateCellVisualLength(text);
      maxLen = Math.max(maxLen, len);
      sumLen += len;
      count += 1;
    });
    const avgLen = count ? sumLen / count : maxLen;
    let weight = maxLen * 0.72 + avgLen * 0.55 + 6;

    if (isSerialColumn(label, col)) {
      weight = cols >= 8 ? 4.2 : 5.2;
    } else if (isDetailColumn(label)) {
      weight *= 2.15;
    } else if (isItemColumn(label)) {
      weight *= 1.05;
    } else if (isTerseColumn(label)) {
      weight *= 0.85;
    }

    weight = Math.max(3.8, Math.min(80, weight));
    return weight;
  });

  const total = weights.reduce((acc, item) => acc + item, 0) || 1;
  const minPct = cols >= 10 ? 4.2 : cols >= 8 ? 5.0 : 6.0;
  const maxPct = cols >= 10 ? 33 : 40;
  const itemMaxPct = cols >= 10 ? 14 : cols >= 8 ? 16 : cols >= 6 ? 20 : 28;
  let percents = weights.map((w) => (w / total) * 100);
  percents = percents.map((pct, idx) => {
    if (isSerialColumn(labels[idx], idx)) {
      return Math.max(3.8, Math.min(8.0, pct));
    }
    if (isItemColumn(labels[idx])) {
      return Math.max(minPct, Math.min(itemMaxPct, pct));
    }
    return Math.max(minPct, Math.min(maxPct, pct));
  });

  const detailIndexes = labels
    .map((label, idx) => (isDetailColumn(label) ? idx : -1))
    .filter((idx) => idx >= 0);
  if (detailIndexes.length) {
    const targetCombined = Math.min(detailIndexes.length * (cols >= 10 ? 18 : cols >= 8 ? 20 : 22), 62);
    const currentCombined = detailIndexes.reduce((acc, idx) => acc + percents[idx], 0);
    if (currentCombined < targetCombined) {
      const deficit = targetCombined - currentCombined;
      const donorIndexes = labels
        .map((label, idx) => ({ idx, label }))
        .filter(({ idx }) => !detailIndexes.includes(idx) && !isSerialColumn(labels[idx], idx))
        .map(({ idx }) => idx);
      const available = donorIndexes.map((idx) => {
        const floor = isTerseColumn(labels[idx]) ? Math.max(4.0, minPct - 1.0) : minPct;
        return Math.max(0, percents[idx] - floor);
      });
      const totalAvailable = available.reduce((acc, item) => acc + item, 0);
      if (totalAvailable > 0.01) {
        donorIndexes.forEach((idx, offset) => {
          percents[idx] -= deficit * (available[offset] / totalAvailable);
        });
        const extraPerDetail = deficit / detailIndexes.length;
        detailIndexes.forEach((idx) => {
          percents[idx] += extraPerDetail;
        });
      }
    }
  }

  rebalanceContentColumns(percents, labels, minPct, maxPct);

  let sum = percents.reduce((acc, item) => acc + item, 0) || 1;
  if (Math.abs(sum - 100) < 0.01) {
    return percents;
  }
  const adjustable = percents
    .map((pct, idx) => ({ idx, pct, serial: isSerialColumn(labels[idx], idx) }))
    .filter((item) => !item.serial);
  if (!adjustable.length) {
    return percents.map((pct) => (pct / sum) * 100);
  }
  const delta = 100 - sum;
  const perCol = delta / adjustable.length;
  adjustable.forEach((item) => {
    const next = percents[item.idx] + perCol;
    const bounded = Math.max(minPct, Math.min(maxPct, next));
    percents[item.idx] = bounded;
  });
  sum = percents.reduce((acc, item) => acc + item, 0) || 1;
  return percents.map((pct) => (pct / sum) * 100);
}

function estimateCellVisualLength(text) {
  const value = String(text || "");
  let score = 0;
  for (const ch of value) {
    if (/[\u4e00-\u9fff]/.test(ch)) {
      score += 2;
    } else {
      score += 1;
    }
  }
  return score;
}

function isSerialColumn(label, index) {
  const value = String(label || "").trim().toLowerCase();
  if (index === 0) {
    return true;
  }
  return /^(?:序号|编号|no\.?|id)$/.test(value);
}

function isDetailColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(要求|描述|内容|说明|备注|方法|条件|requirements?|description|detail|method|procedure|测试条件|试验内容|试验方法)/i.test(
    value
  );
}

function isItemColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(项目|item|测试项目|试验项目|类别|category|test item)/i.test(value);
}

function isTerseColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(次数|时间|天|结果|状态|结论|周期|sample|qty|数量|顺序|对象|责任方|样件|dv\/pv|标准)/i.test(value);
}

function rebalanceContentColumns(percents, labels, minPct, maxPct) {
  const detailIndexes = labels
    .map((label, idx) => (isDetailColumn(label) ? idx : -1))
    .filter((idx) => idx >= 0);
  if (!detailIndexes.length) {
    return;
  }

  const primaryIndexes = labels
    .map((label, idx) => (isPrimaryDetailColumn(label) ? idx : -1))
    .filter((idx) => idx >= 0);
  const donorIndexes = labels
    .map((label, idx) => idx)
    .filter((idx) => !detailIndexes.includes(idx) && !isSerialColumn(labels[idx], idx));
  if (!donorIndexes.length) {
    return;
  }

  const desiredDetailMin = detailIndexes.length >= 2 ? 46 : 30;
  const desiredPrimaryMin = primaryIndexes.length >= 2 ? 36 : primaryIndexes.length ? 22 : 0;
  shiftWidthToTargets(percents, labels, donorIndexes, detailIndexes, desiredDetailMin, minPct);
  shiftWidthToTargets(percents, labels, donorIndexes, primaryIndexes, desiredPrimaryMin, minPct);

  percents.forEach((pct, idx) => {
    if (isPrimaryDetailColumn(labels[idx])) {
      percents[idx] = Math.min(maxPct, Math.max(colsAwareFloor(labels.length, idx, labels), pct));
    }
  });
}

function shiftWidthToTargets(percents, labels, donorIndexes, targetIndexes, desiredMin, minPct) {
  if (!targetIndexes.length) {
    return;
  }
  const current = targetIndexes.reduce((acc, idx) => acc + percents[idx], 0);
  if (current >= desiredMin) {
    return;
  }
  const deficit = desiredMin - current;
  const availability = donorIndexes.map((idx) => {
    const floor = isTerseColumn(labels[idx]) ? Math.max(4.0, minPct - 1.0) : minPct;
    return Math.max(0, percents[idx] - floor);
  });
  const totalAvailable = availability.reduce((acc, item) => acc + item, 0);
  if (totalAvailable <= 0.01) {
    return;
  }
  donorIndexes.forEach((idx, offset) => {
    percents[idx] -= deficit * (availability[offset] / totalAvailable);
  });
  const targetWeights = targetIndexes.map((idx) => (isPrimaryDetailColumn(labels[idx]) ? 1.2 : 1.0));
  const targetTotal = targetWeights.reduce((acc, item) => acc + item, 0) || 1;
  targetIndexes.forEach((idx, offset) => {
    percents[idx] += deficit * (targetWeights[offset] / targetTotal);
  });
}

function colsAwareFloor(colCount, index, labels) {
  if (isSerialColumn(labels[index], index)) {
    return 3.8;
  }
  if (isPrimaryDetailColumn(labels[index])) {
    return colCount >= 10 ? 14 : colCount >= 8 ? 16 : 18;
  }
  if (isDetailColumn(labels[index])) {
    return colCount >= 10 ? 10 : colCount >= 8 ? 12 : 14;
  }
  return colCount >= 10 ? 4.2 : colCount >= 8 ? 5 : 6;
}

function isPrimaryDetailColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(试验方法|测试方法|方法|procedure|method|要求|需求|criteria|requirement)/i.test(value);
}

function detectMergeColumns(head) {
  const header = (head || []).map((cell) => String(cell || "").trim());
  if (!header.length) {
    return [];
  }
  const joined = header.join("|");
  if (joined.includes("测试类别") && joined.includes("测试小类")) {
    const cols = [];
    const categoryIndex = header.findIndex((cell) => cell.includes("测试类别"));
    const subcategoryIndex = header.findIndex((cell) => cell.includes("测试小类"));
    if (categoryIndex > 0) {
      cols.push(categoryIndex);
    }
    if (subcategoryIndex > 0) {
      cols.push(subcategoryIndex);
    }
    return cols;
  }
  return [];
}

function canApplyMergeColumns(head, body, colCount) {
  const cols = Number(colCount) || 0;
  if (!cols || cols > 6) {
    return false;
  }
  if (!Array.isArray(body) || !body.length || body.length > 160) {
    return false;
  }
  const sampleRows = body.slice(0, 48);
  const hasHeavyCells = sampleRows.some((row) =>
    (row || []).some((cell) => estimateCellVisualLength(cell) >= 140)
  );
  if (hasHeavyCells && cols >= 5) {
    return false;
  }
  const headerText = (head || []).map((cell) => String(cell || "")).join("|");
  if (/requirements?|要求|试验内容|描述/i.test(headerText) && cols >= 5) {
    return false;
  }
  return true;
}

function mergeBodyCellsByColumn(body, columns) {
  const matrix = (body || []).map((row) =>
    row.map((text) => ({ text: String(text || ""), rowspan: 1, skip: false }))
  );
  if (!matrix.length) {
    return matrix;
  }

  columns.forEach((columnIndex) => {
    if (columnIndex <= 0) {
      return;
    }
    let start = 0;
    while (start < matrix.length) {
      const first = matrix[start]?.[columnIndex];
      if (!first || first.skip) {
        start += 1;
        continue;
      }
      const token = normalizeMergeToken(first.text);
      if (!token) {
        start += 1;
        continue;
      }
      let end = start + 1;
      while (end < matrix.length) {
        const current = matrix[end]?.[columnIndex];
        if (!current || current.skip) {
          break;
        }
        if (normalizeMergeToken(current.text) !== token) {
          break;
        }
        end += 1;
      }
      if (end - start > 1) {
        first.rowspan = end - start;
        for (let i = start + 1; i < end; i += 1) {
          matrix[i][columnIndex].skip = true;
        }
      }
      start = end;
    }
  });
  return matrix;
}

function normalizeMergeToken(value) {
  const text = String(value || "").trim();
  if (!text || text === "/" || /^[-–—]+$/.test(text)) {
    return "";
  }
  return text.replace(/\s+/g, " ");
}

function splitMarkdownTableChunks(lines) {
  if (!Array.isArray(lines) || !lines.length) {
    return [];
  }
  const chunks = [];
  let current = [];
  let hasDataRows = false;

  lines.forEach((line) => {
    const cells = parseMarkdownCells(line);
    if (!cells.length) {
      return;
    }

    const headerRow = isLikelyTableHeaderRow(cells);
    const shouldBreak = current.length > 0 && headerRow && hasDataRows;

    if (shouldBreak) {
      chunks.push(current);
      current = [];
      hasDataRows = false;
    }

    current.push(line);
    if (!headerRow) {
      hasDataRows = true;
    }
  });

  if (current.length) {
    chunks.push(current);
  }
  return chunks;
}

function looksLikeLowConfidenceTable(lines) {
  if (!Array.isArray(lines) || !lines.length) {
    return false;
  }
  const headerLine = lines.find((line) =>
    /序号/.test(String(line || "")) &&
    /(项目|测试类别|测试小类|测试项目|requirements|test item)/i.test(String(line || ""))
  );
  if (!headerLine) {
    return false;
  }
  const dataLikeCount = lines.filter((line) => /^\s*\d{1,3}\s+/.test(String(line || ""))).length;
  return dataLikeCount >= 3;
}

function renderLowConfidenceTable(lines) {
  const rows = [];
  lines.forEach((raw) => {
    const line = String(raw || "").trim();
    if (!line || /序号/.test(line)) {
      return;
    }
    const match = line.match(/^(\d{1,3})\s+(.+)$/);
    if (!match) {
      return;
    }
    rows.push([match[1], match[2]]);
  });
  if (rows.length < 2) {
    return "";
  }
  const body = rows
    .map((row) => `<tr><td>${escapeHtml(row[0])}</td><td>${escapeHtml(row[1])}</td></tr>`)
    .join("");
  return `<div class="md-table-wrap"><table class="md-table md-table-accurate"><thead><tr><th>序号</th><th>内容</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function isLikelyTableHeaderRow(cells) {
  const joined = cells.join("|").toLowerCase();
  if (joined.includes("序号") && joined.includes("项目")) {
    return true;
  }
  if (joined.includes("test item") && joined.includes("requirements")) {
    return true;
  }
  return false;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatScope(scope, title) {
  const normalizedTitle = String(title || "").trim();
  if (normalizedTitle) {
    return normalizedTitle;
  }
  const normalized = String(scope || "MAIN").trim().toUpperCase();
  return normalized || "MAIN";
}

function formatTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatWorkflowStage(value) {
  const token = String(value || "").trim().toUpperCase();
  const labels = {
    UPLOADED: "已上传",
    EXTRACTED: "已提取",
    REVIEWING: "审阅中",
    EXTRACTION_LOCKED: "条目已锁定",
    AI_ANALYZING: "AI 分析中",
    AI_READY: "AI 已完成",
  };
  return labels[token] || token || "-";
}

function formatAiStatus(value, stale = false) {
  const token = String(value || "").trim().toUpperCase() || "NOT_RUN";
  if (stale || token === "STALE") {
    return "结果过期";
  }
  const labels = {
    NOT_RUN: "未运行",
    RUNNING: "运行中",
    READY: "已完成",
  };
  return labels[token] || token;
}

async function api(url, options = {}) {
  const next = { ...options };
  const method = String(next.method || "GET").toUpperCase();
  let requestUrl = String(url || "");
  if (method === "GET") {
    const sep = requestUrl.includes("?") ? "&" : "?";
    requestUrl = `${requestUrl}${sep}_ts=${Date.now()}`;
    next.cache = "no-store";
  }
  const response = await fetch(requestUrl, next);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`请求失败(${response.status}): ${text}`);
  }
  return response.json();
}

function wrap(handler) {
  return async (event) => {
    try {
      await handler(event);
    } catch (error) {
      console.error(error);
      alert(error.message || "操作失败");
    }
  };
}
