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
  showRawDuplicateRows: false,
  selectedKinds: new Set(),
  rawViewByEntryId: {},
  pdfBaseUrl: "",
  pdfCurrentPage: 1,
  activeEntryId: "",
  pdfViewerReady: false,
  pendingPdfCommand: null,
  pdfDrawerOpen: false,
};
const FRONTEND_BUILD = "20260306-3";
const PDF_VIEWER_VERSION = "20260306-3";

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
  bindEvents();
  syncPdfDrawerState();
  resetPdfViewer("点击左侧条目后，这里会跳转到对应页。");
  await loadProjects();
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
  el.requirementBody.addEventListener("click", onRequirementTableClicked);
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
  await loadDocuments();
  clearRendered();
}

function onDocumentChanged() {
  state.documentId = el.documentSelect.value;
  state.preview = null;
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
  state.showRawDuplicateRows = false;
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
    buildSectionOverviewRows(
      data.preview?.section_tree || data.preview?.section_outline || [],
      data.preview?.sections || [],
      data.review_entries || []
    )
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
    ["章节树节点", summary.section_outline_count ?? 0],
    ["章节一致性问题", summary.section_consistency_issues_total ?? 0],
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
  }，需求条目 ${summary.requirements_count || 0}，一致性问题 ${summary.section_consistency_issues_total || 0}，低置信页 ${summary.low_confidence_page_count || 0}。`;
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

function buildSectionOverviewRows(outlineRows, sectionRows, requirements) {
  const rowsByKey = new Map();

  (sectionRows || []).forEach((row, index) => {
    const key = buildSectionKey(row);
    if (!key) {
      return;
    }
    rowsByKey.set(key, {
      appendix_scope: row.appendix_scope,
      section_code: row.section_code || "",
      section_title: row.section_title || "",
      section_level: Number(row.section_level || 0),
      parent_section_code: row.parent_section_code || "",
      source_page_start: row.source_page_start,
      source_page_end: row.source_page_end,
      requirement_count: row.requirement_count ?? 0,
      total_chars: row.total_chars ?? row.section_char_count ?? row.char_count ?? 0,
      requirement_like: row.requirement_like,
      outline_order: Number.MAX_SAFE_INTEGER,
      section_order: index,
    });
  });

  (outlineRows || []).forEach((row, index) => {
    const key = buildSectionKey(row);
    if (!key) {
      return;
    }
    const existing = rowsByKey.get(key) || {
      appendix_scope: row.appendix_scope,
      section_code: row.section_code || "",
      section_title: "",
      section_level: 0,
      parent_section_code: "",
      source_page_start: null,
      source_page_end: null,
      requirement_count: 0,
      total_chars: 0,
      requirement_like: false,
      outline_order: Number.MAX_SAFE_INTEGER,
      section_order: Number.MAX_SAFE_INTEGER,
    };
    rowsByKey.set(key, {
      ...existing,
      appendix_scope: row.appendix_scope ?? existing.appendix_scope,
      section_code: row.section_code || existing.section_code,
      section_title: row.section_title || existing.section_title,
      section_level: Number(row.section_level || existing.section_level || 0),
      parent_section_code: row.parent_section_code || existing.parent_section_code,
      source_page_start: row.source_page_start ?? existing.source_page_start,
      source_page_end: row.source_page_end ?? existing.source_page_end,
      total_chars:
        row.char_count ??
        row.section_char_count ??
        existing.total_chars ??
        0,
      requirement_like:
        typeof row.requirement_like === "boolean"
          ? row.requirement_like
          : existing.requirement_like,
      outline_order: index,
    });
  });

  mergeRequirementOnlySectionRows(rowsByKey, requirements || []);

  return [...rowsByKey.values()].sort(compareSectionOverviewRows);
}

function renderSectionOverviewTable(rows) {
  if (!rows.length) {
    el.sectionOverviewBody.innerHTML = `<tr><td colspan="9">未识别到章节</td></tr>`;
    return;
  }

  el.sectionOverviewBody.innerHTML = rows
    .map((row) => {
      const span = `${row.source_page_start ?? ""}-${row.source_page_end ?? ""}`;
      const level = Number(row.section_level || 0);
      const indent = level > 1 ? "&nbsp;".repeat((level - 1) * 4) : "";
      return `<tr>
        <td>${escapeHtml(formatScope(row.appendix_scope))}</td>
        <td>${indent}${escapeHtml(row.section_code || "")}</td>
        <td>${escapeHtml(row.section_title || "")}</td>
        <td>${level || ""}</td>
        <td>${escapeHtml(row.parent_section_code || "")}</td>
        <td>${escapeHtml(span)}</td>
        <td>${row.requirement_count ?? 0}</td>
        <td>${row.total_chars ?? 0}</td>
        <td>${row.requirement_like ? "是" : "否"}</td>
      </tr>`;
    })
    .join("");
}

function buildSectionKey(row) {
  const scope = String(row?.appendix_scope || "MAIN").trim().toUpperCase() || "MAIN";
  const sectionCode = String(row?.section_code || "").trim();
  const title = normalizeSectionTitle(row?.section_title || "");
  const level = Number(row?.section_level || 0);
  const instanceKey = String(row?.section_instance_key || "").trim();
  if (instanceKey) {
    const normInstance = instanceKey.toUpperCase();
    const normCode = sectionCode.toUpperCase();
    const normScopedCode = normCode ? `${scope}:${normCode}` : "";
    const isBaseInstance = Boolean(normCode) && (normInstance === normCode || normInstance === normScopedCode);
    // Keep non-base instances (e.g. duplicate numeric code with different title via "#...").
    if (!isBaseInstance || instanceKey.includes("#")) {
      return `${scope}::${instanceKey}`;
    }
  }

  // Keep appendix/main heading rows even when code is empty.
  if (!sectionCode && !title) {
    return "";
  }
  // Avoid collapsing duplicated numeric section codes with different titles (e.g. U:63 + U:63 EMC).
  return `${scope}::${sectionCode}::${title}::L${level}`;
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
  const left = String(a || "").trim().toUpperCase();
  const right = String(b || "").trim().toUpperCase();
  if (left === right) {
    return 0;
  }
  if (left === "MAIN") {
    return -1;
  }
  if (right === "MAIN") {
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

function mergeRequirementOnlySectionRows(rowsByKey, requirements) {
  const grouped = new Map();
  (requirements || []).forEach((row, index) => {
    const sectionCode = String(row?.section_code || "").trim();
    const sectionTitle = String(row?.section_title || "").trim();
    if (!sectionCode && !sectionTitle) {
      return;
    }
    const sectionLevel =
      Number(row?.section_level || 0) || inferSectionLevel(sectionCode);
    const candidate = {
      appendix_scope: row?.appendix_scope || "",
      section_code: sectionCode,
      section_title: sectionTitle,
      section_level: sectionLevel,
    };
    const key = buildSectionKey(candidate);
    if (!key) {
      return;
    }
    const sourcePage = Number(row?.source_page ?? 0) || 0;
    const sourcePageEnd = Number(row?.source_page_end ?? sourcePage) || sourcePage;
    const textLength = Number(row?.text_length ?? 0) || 0;
    const current = grouped.get(key) || {
      ...candidate,
      source_page_start: sourcePage || null,
      source_page_end: sourcePageEnd || null,
      requirement_count: 0,
      total_chars: 0,
      requirement_like: true,
      req_order: index,
    };
    current.requirement_count += 1;
    current.total_chars += textLength;
    if (sourcePage && (current.source_page_start === null || sourcePage < current.source_page_start)) {
      current.source_page_start = sourcePage;
    }
    if (sourcePageEnd && (current.source_page_end === null || sourcePageEnd > current.source_page_end)) {
      current.source_page_end = sourcePageEnd;
    }
    grouped.set(key, current);
  });

  grouped.forEach((agg, key) => {
    const existing = rowsByKey.get(key);
    if (existing) {
      existing.requirement_count = Math.max(existing.requirement_count ?? 0, agg.requirement_count);
      existing.total_chars = Math.max(existing.total_chars ?? 0, agg.total_chars ?? 0);
      if (!existing.source_page_start && agg.source_page_start) {
        existing.source_page_start = agg.source_page_start;
      }
      if (!existing.source_page_end && agg.source_page_end) {
        existing.source_page_end = agg.source_page_end;
      }
      if (existing.requirement_like == null) {
        existing.requirement_like = true;
      }
      return;
    }
    rowsByKey.set(key, {
      appendix_scope: agg.appendix_scope,
      section_code: agg.section_code,
      section_title: agg.section_title,
      section_level: agg.section_level,
      parent_section_code: "",
      source_page_start: agg.source_page_start,
      source_page_end: agg.source_page_end,
      requirement_count: agg.requirement_count,
      total_chars: agg.total_chars,
      requirement_like: true,
      outline_order: Number.MAX_SAFE_INTEGER,
      section_order: Number.MAX_SAFE_INTEGER - 1,
      req_order: agg.req_order,
    });
  });
}

function inferSectionLevel(sectionCode) {
  const code = String(sectionCode || "").trim();
  if (!code) {
    return 0;
  }
  if (/^\d+(?:\.\d+){0,8}$/.test(code)) {
    return code.split(".").length;
  }
  return 1;
}

function ensureMainRootRow(rowsByKey) {
  const rows = [...rowsByKey.values()];
  const hasMainSections = rows.some(
    (row) => String(row.appendix_scope || "MAIN").trim().toUpperCase() === "MAIN"
  );
  if (!hasMainSections) {
    return;
  }
  const hasMainRoot = rows.some((row) => {
    const scope = String(row.appendix_scope || "MAIN").trim().toUpperCase();
    const code = String(row.section_code || "").trim();
    return scope === "MAIN" && !code;
  });
  if (hasMainRoot) {
    return;
  }
  const firstMainRow = rows.find(
    (row) => String(row.appendix_scope || "MAIN").trim().toUpperCase() === "MAIN"
  );
  const root = {
    appendix_scope: "",
    section_code: "",
    section_title: "正文（Main）",
    section_level: 0,
    parent_section_code: "",
    source_page_start: firstMainRow?.source_page_start ?? "",
    source_page_end: firstMainRow?.source_page_end ?? "",
    requirement_count: 0,
    total_chars: 0,
    requirement_like: false,
    outline_order: -1,
    section_order: -1,
  };
  rowsByKey.set(buildSectionKey(root), root);
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
        <td>${escapeHtml(row.issue_type || "")}</td>
        <td>${escapeHtml(row.severity || "")}</td>
        <td>${escapeHtml(formatScope(row.appendix_scope))}</td>
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
        }${renderRequirementContent({ ...row, raw_text: textValue, display_text: textValue, requirement_text: textValue })}</td>
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

function onRequirementTableClicked(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
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
      return renderTableRef(tableRefs.get(String(block?.table_id || "")));
    })
    .filter(Boolean);
  return rendered.join("") || renderRichRequirementText(row?.raw_text || row?.display_text || row?.requirement_text || "");
}

function renderTableRef(ref) {
  if (!ref) {
    return "";
  }
  const headers = Array.isArray(ref.inherited_headers) ? ref.inherited_headers : [];
  const rows = Array.isArray(ref.slice_rows) ? ref.slice_rows.filter((row) => Array.isArray(row)) : [];
  if (String(ref.slice_render_mode || "") === "structured" && headers.length) {
    return renderStructuredTable(headers, rows);
  }
  if (ref.slice_snapshot_url) {
    return `<div class="asset-snapshot-wrap"><img class="asset-snapshot" src="${escapeHtml(
      ref.slice_snapshot_url
    )}" alt="table snapshot" loading="lazy" /></div>`;
  }
  return `<div class="md-text-block">${escapeHtml(String(ref.slice_ai_projection || "")).replaceAll("\n", "<br>")}</div>`;
}

function renderStructuredTable(headers, rows) {
  const head = headers.map((cell) => `<th>${escapeHtml(String(cell || ""))}</th>`).join("");
  const body = rows
    .map(
      (row) =>
        `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell || ""))}</td>`).join("")}</tr>`
    )
    .join("");
  return `<div class="md-table-wrap asset-table-wrap"><table class="md-table asset-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
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
  const maxPct = cols >= 10 ? 35 : 42;
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
  return /(要求|描述|内容|说明|备注|requirements?|description|detail|测试条件|试验内容)/i.test(value);
}

function isItemColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(项目|item|测试项目|试验项目|类别|category|test item)/i.test(value);
}

function isTerseColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(次数|时间|天|结果|状态|结论|周期|sample|qty|数量|顺序|对象)/i.test(value);
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

function formatScope(scope) {
  const normalized = String(scope || "").trim().toUpperCase();
  return normalized || "Main";
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
