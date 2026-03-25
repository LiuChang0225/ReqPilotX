const state = {
  projectId: null,
  sorDocuments: [],
  currentSorDocumentId: null,
  currentStage: "review",
  workspace: null,
  reviewPreview: null,
  requirements: [],
  reviewEntries: [],
  reviewSelectedKind: "",
  reviewShowRecommendedSourceOnly: false,
  reviewActiveEntryId: "",
  reviewPdfBaseUrl: "",
  reviewPdfCurrentPage: 1,
  reviewPdfViewerReady: false,
  reviewPendingPdfCommand: null,
  reviewPdfDrawerOpen: false,
  exportJobs: [],
  lockInFlight: false,
  exportInFlight: false,
  reviewBulkApplyInFlight: false,
  reviewPreviewInFlight: false,
  reviewPreviewPollHandle: null,
  exportStatusMessage: "",
  exportStatusTone: "",
  tableViewByTableId: {},
};
const WORKSPACE_SELECTION_KEY = "reqpilotx.workspace-selection-v1";

const el = {
  projectForm: document.querySelector("#project-form"),
  projectName: document.querySelector("#project-name"),
  customerName: document.querySelector("#customer-name"),
  projectSelect: document.querySelector("#project-select"),
  sorDocumentSelect: document.querySelector("#sor-document-select"),
  refreshProjects: document.querySelector("#refresh-projects"),
  sorForm: document.querySelector("#sor-upload-form"),
  sorFile: document.querySelector("#sor-file"),
  sorUploadResult: document.querySelector("#sor-upload-result"),
  kbForm: document.querySelector("#kb-upload-form"),
  kbFile: document.querySelector("#kb-file"),
  kbUploadResult: document.querySelector("#kb-upload-result"),
  analyzeBtn: document.querySelector("#analyze-btn"),
  lockExtractionBtn: document.querySelector("#lock-extraction-btn"),
  runAiBtn: document.querySelector("#run-ai-btn"),
  reloadBtn: document.querySelector("#reload-btn"),
  analyzeResult: document.querySelector("#analyze-result"),
  exportBtn: document.querySelector("#export-btn"),
  exportVersionLabel: document.querySelector("#export-version-label"),
  exportCompareSelect: document.querySelector("#export-compare-select"),
  refreshExportsBtn: document.querySelector("#refresh-exports-btn"),
  downloadLink: document.querySelector("#download-link"),
  exportStatus: document.querySelector("#export-status"),
  matrixStatus: document.querySelector("#matrix-status"),
  workspaceSummary: document.querySelector("#workspace-summary"),
  workspaceStatus: document.querySelector("#workspace-status"),
  batchDecision: document.querySelector("#batch-decision"),
  batchReasonTemplate: document.querySelector("#batch-reason-template"),
  batchFilterSection: document.querySelector("#batch-filter-section"),
  batchFilterKeyword: document.querySelector("#batch-filter-keyword"),
  batchOnlyEmpty: document.querySelector("#batch-only-empty"),
  batchApplyBtn: document.querySelector("#batch-apply-btn"),
  batchResult: document.querySelector("#batch-result"),
  tableBody: document.querySelector("#requirements-table tbody"),
  stageSwitcher: document.querySelector("#stage-switcher"),
  reviewStagePanel: document.querySelector("#review-stage-panel"),
  aiStagePanel: document.querySelector("#ai-stage-panel"),
  matrixStagePanel: document.querySelector("#matrix-stage-panel"),
  matrixPanel: document.querySelector("#matrix-panel"),
  reviewLayout: document.querySelector("#review-layout"),
  reviewKindFilter: document.querySelector("#review-kind-filter"),
  reviewSectionFilter: document.querySelector("#review-section-filter"),
  reviewKeywordFilter: document.querySelector("#review-keyword-filter"),
  reviewVisibleKeepToggle: document.querySelector("#review-visible-keep-toggle"),
  reviewSelectAllBtn: document.querySelector("#review-select-all-btn"),
  reviewUnselectAllBtn: document.querySelector("#review-unselect-all-btn"),
  reviewSourceRecommendedBtn: document.querySelector("#review-source-recommended-btn"),
  reviewRefreshPreviewBtn: document.querySelector("#review-refresh-preview-btn"),
  reviewDiagnosticCards: document.querySelector("#review-diagnostic-cards"),
  reviewSectionOverviewBody: document.querySelector("#review-section-overview-body"),
  reviewTableWrap: document.querySelector(".review-table-wrap"),
  reviewTableBody: document.querySelector("#review-table-body"),
  reviewStatus: document.querySelector("#review-status"),
  reviewPdfDrawer: document.querySelector("#review-pdf-drawer"),
  reviewPdfToggle: document.querySelector("#review-pdf-toggle"),
  reviewOpenPdfBtn: document.querySelector("#review-open-pdf-btn"),
  reviewPdfHint: document.querySelector("#review-pdf-hint"),
  reviewPdfFrame: document.querySelector("#review-pdf-frame"),
  aiStageChecklist: document.querySelector("#ai-stage-checklist"),
  aiStageStatus: document.querySelector("#ai-stage-status"),
};

init().catch((error) => {
  console.error(error);
  alert(error.message || "初始化失败，请检查后端是否已启动。");
});

async function init() {
  restoreWorkspaceSelection();
  bindEvents();
  syncStageFromUrl();
  syncStagePanels();
  await loadProjects();
}

function restoreWorkspaceSelection() {
  try {
    const payload = JSON.parse(window.localStorage.getItem(WORKSPACE_SELECTION_KEY) || "{}");
    if (payload && typeof payload === "object") {
      state.projectId = payload.projectId || null;
      state.currentSorDocumentId = payload.documentId || null;
    }
  } catch (_error) {
    state.projectId = state.projectId || null;
    state.currentSorDocumentId = state.currentSorDocumentId || null;
  }
}

function persistWorkspaceSelection() {
  try {
    window.localStorage.setItem(
      WORKSPACE_SELECTION_KEY,
      JSON.stringify({
        projectId: state.projectId || "",
        documentId: state.currentSorDocumentId || "",
      })
    );
  } catch (_error) {
    // Best effort only.
  }
}

function onStageSwitcherClicked(event) {
  const button = event.target.closest("[data-stage]");
  if (!(button instanceof HTMLButtonElement)) {
    return;
  }
  const nextStage = String(button.getAttribute("data-stage") || "").trim();
  if (!nextStage) {
    return;
  }
  state.currentStage = nextStage;
  syncStagePanels();
  syncStageUrl();
  syncWorkspaceActions();
  if (nextStage === "review") {
    wrap(loadReviewPreview)();
  } else {
    stopReviewPreviewPolling();
  }
}

function syncStageFromUrl() {
  const stage = new URLSearchParams(window.location.search).get("stage");
  if (stage && ["review", "ai", "matrix"].includes(stage)) {
    state.currentStage = stage;
  }
}

function syncStageUrl() {
  const url = new URL(window.location.href);
  url.searchParams.set("stage", state.currentStage || "review");
  window.history.replaceState({}, "", url);
}

function bindEvents() {
  el.projectForm.addEventListener("submit", wrap(onCreateProject));
  el.refreshProjects.addEventListener("click", wrap(loadProjects));
  el.projectSelect.addEventListener("change", wrap(onProjectChanged));
  el.sorDocumentSelect.addEventListener("change", wrap(onSorDocumentChanged));
  el.sorForm.addEventListener("submit", wrap(onUploadSor));
  el.kbForm.addEventListener("submit", wrap(onUploadKnowledge));
  el.analyzeBtn.addEventListener("click", wrap(onAnalyze));
  el.lockExtractionBtn.addEventListener("click", wrap(onLockExtraction));
  el.runAiBtn.addEventListener("click", wrap(onRunAiAnalysis));
  el.reloadBtn.addEventListener("click", wrap(onReload));
  el.exportBtn.addEventListener("click", wrap(onExport));
  el.refreshExportsBtn.addEventListener("click", wrap(loadExportJobs));
  el.batchApplyBtn.addEventListener("click", wrap(onApplyBatchDecision));
  el.stageSwitcher?.addEventListener("click", onStageSwitcherClicked);
  el.reviewKindFilter?.addEventListener("change", onReviewKindFilterChanged);
  el.reviewSectionFilter?.addEventListener("input", renderReviewEntries);
  el.reviewKeywordFilter?.addEventListener("input", renderReviewEntries);
  el.reviewVisibleKeepToggle?.addEventListener("change", wrap(onReviewVisibleKeepToggleChanged));
  el.reviewSelectAllBtn?.addEventListener("click", wrap(() => bulkSetReviewKeep(true)));
  el.reviewUnselectAllBtn?.addEventListener("click", wrap(() => bulkSetReviewKeep(false)));
  el.reviewSourceRecommendedBtn?.addEventListener("click", toggleReviewRecommendedSourceOnly);
  el.reviewRefreshPreviewBtn?.addEventListener("click", wrap(onRefreshReviewPreview));
  el.reviewTableBody?.addEventListener("change", wrap(onReviewTableChanged));
  el.reviewTableBody?.addEventListener("click", wrap(onReviewTableClicked));
  el.tableBody?.addEventListener("click", onRequirementTableClicked);
  el.reviewPdfToggle?.addEventListener("click", onReviewPdfToggleClicked);
  el.reviewOpenPdfBtn?.addEventListener("click", openReviewPdfInNewWindow);
  window.addEventListener("message", onReviewPdfViewerMessage);
}

async function onCreateProject(event) {
  event.preventDefault();
  const payload = {
    name: el.projectName.value.trim(),
    customer_name: el.customerName.value.trim(),
  };
  if (!payload.name || !payload.customer_name) {
    alert("请填写项目名称和客户名称");
    return;
  }
  await api("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  el.projectName.value = "";
  el.customerName.value = "";
  await loadProjects();
}

async function onProjectChanged() {
  state.projectId = el.projectSelect.value || null;
  state.currentSorDocumentId = null;
  state.sorDocuments = [];
  state.workspace = null;
  state.reviewPreview = null;
  state.exportStatusMessage = "";
  state.exportStatusTone = "";
  state.tableViewByTableId = {};
  el.sorUploadResult.textContent = "";
  el.kbUploadResult.textContent = "";
  setAnalyzeStatus("");
  el.downloadLink.textContent = "";
  el.downloadLink.removeAttribute("href");
  persistWorkspaceSelection();
  if (state.projectId) {
    await loadSorDocuments();
    await Promise.all([loadWorkspace(), loadExportJobs()]);
  } else {
    renderRequirements([]);
    renderExportOptions([]);
    renderWorkspaceSummary();
    renderReviewDiagnostics();
    renderReviewEntries();
    syncWorkspaceActions();
  }
}

async function onSorDocumentChanged() {
  state.currentSorDocumentId = el.sorDocumentSelect.value || null;
  state.reviewPreview = null;
  state.exportStatusMessage = "";
  state.exportStatusTone = "";
  state.tableViewByTableId = {};
  persistWorkspaceSelection();
  await loadWorkspace();
}

async function onUploadSor(event) {
  event.preventDefault();
  if (!state.projectId) {
    alert("请先选择项目");
    return;
  }
  const file = el.sorFile.files?.[0];
  if (!file) {
    alert("请选择SOR文件");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const document = await api(`/api/projects/${state.projectId}/sor-documents`, {
    method: "POST",
    body: form,
  });
  await loadSorDocuments(document.id);
  await loadWorkspace();
  el.sorUploadResult.textContent = `已上传: ${document.file_name}`;
  el.sorFile.value = "";
}

async function onUploadKnowledge(event) {
  event.preventDefault();
  if (!state.projectId) {
    alert("请先选择项目");
    return;
  }
  const file = el.kbFile.files?.[0];
  if (!file) {
    alert("请选择知识文档");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const document = await api(`/api/projects/${state.projectId}/knowledge-documents`, {
    method: "POST",
    body: form,
  });
  el.kbUploadResult.textContent = `已上传: ${document.file_name}，知识库版本 ${document.knowledge_base_version}`;
  el.kbFile.value = "";
  await loadWorkspace();
}

async function onAnalyze() {
  if (!state.projectId) {
    alert("请先选择项目");
    return;
  }
  if (state.currentStage !== "review") {
    alert("请先切换到“条目审阅”阶段，再生成条目集。");
    return;
  }
  if (!state.currentSorDocumentId) {
    await loadSorDocuments();
  }
  if (!state.currentSorDocumentId) {
    alert("请先上传SOR文档");
    return;
  }
  if (requiresAnalyzeResetConfirmation()) {
    const accepted = window.confirm("重新生成会清空锁定状态，并使现有 AI 结果失效。是否继续？");
    if (!accepted) {
      return;
    }
  }
  el.analyzeBtn.disabled = true;
  setAnalyzeStatus("条目集生成中，请稍候...");
  try {
    const result = await api(`/api/projects/${state.projectId}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: state.currentSorDocumentId }),
    });
    setAnalyzeStatus(`条目集已生成：${result.requirements_generated} 条，版本 ${result.requirement_set_version}，AI 未自动运行。`, "success");
    await loadWorkspace();
  } finally {
    el.analyzeBtn.disabled = false;
    syncWorkspaceActions();
  }
}

async function onReload() {
  if (!state.projectId) {
    return;
  }
  await Promise.all([loadWorkspace(), loadExportJobs()]);
}

async function onLockExtraction() {
  if (!state.projectId || !state.currentSorDocumentId) {
    alert("请先选择项目和 SOR 文档");
    return;
  }
  state.lockInFlight = true;
  syncWorkspaceActions();
  setAnalyzeStatus("条目集锁定中，请稍候...");
  try {
    const workspace = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/lock-extraction`,
      { method: "POST" }
    );
    state.workspace = workspace;
    state.requirements = Array.isArray(workspace.requirements) ? workspace.requirements : [];
    renderRequirements(state.requirements);
    renderWorkspaceSummary();
    syncWorkspaceActions();
    state.exportStatusMessage = "";
    state.exportStatusTone = "";
    setAnalyzeStatus("条目集已锁定，可以开始 AI 分析或导出。", "success");
  } catch (error) {
    setAnalyzeStatus(normalizeApiError(error, "锁定失败，请稍后重试。"), "error");
  } finally {
    state.lockInFlight = false;
    syncWorkspaceActions();
  }
}

async function onRunAiAnalysis() {
  if (!state.projectId || !state.currentSorDocumentId) {
    alert("请先选择项目和 SOR 文档");
    return;
  }
  el.runAiBtn.disabled = true;
  setAnalyzeStatus("AI 分析中，请稍候...");
  try {
    const workspace = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/run-ai-analysis`,
      { method: "POST" }
    );
    state.workspace = workspace;
    state.requirements = Array.isArray(workspace.requirements) ? workspace.requirements : [];
    renderRequirements(state.requirements);
    renderWorkspaceSummary();
    setAnalyzeStatus(
      `AI 分析完成：处理 ${workspace.ai_processed_requirements || 0} 条，知识文档 ${workspace.knowledge_docs_used || 0} 份。`,
      "success"
    );
  } finally {
    syncWorkspaceActions();
  }
}

async function onExport() {
  if (!state.projectId || !state.currentSorDocumentId) {
    alert("请先选择项目和 SOR 文档");
    return;
  }
  if (!Boolean(state.workspace?.actions?.can_export)) {
    state.exportStatusMessage = "导出 Compliance Matrix 前，请先锁定当前条目集。";
    state.exportStatusTone = "error";
    renderExportStatus();
    return;
  }
  state.exportInFlight = true;
  state.exportStatusMessage = "Compliance Matrix 导出中，请稍候...";
  state.exportStatusTone = "info";
  syncWorkspaceActions();
  const payload = {
    version_label: el.exportVersionLabel.value.trim() || null,
    compare_export_id: el.exportCompareSelect.value || null,
    document_id: state.currentSorDocumentId || null,
  };
  try {
    const result = await api(`/api/projects/${state.projectId}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    el.downloadLink.href = result.download_url;
    el.downloadLink.textContent = `下载: ${result.file_name}`;
    state.exportStatusMessage = `导出完成：${result.file_name}`;
    state.exportStatusTone = "success";
    await loadExportJobs();
  } catch (error) {
    const status = getApiErrorStatus(error);
    if (status === 409) {
      state.exportStatusMessage = "条目集已变更，请先重新锁定后再导出。";
      state.exportStatusTone = "error";
      await loadWorkspace();
    } else if (status === 503 || String(error?.message || "").toLowerCase().includes("locked")) {
      state.exportStatusMessage = "系统忙，请重试。";
      state.exportStatusTone = "error";
    } else {
      state.exportStatusMessage = normalizeApiError(error, "导出失败，请稍后重试。");
      state.exportStatusTone = "error";
    }
  } finally {
    state.exportInFlight = false;
    syncWorkspaceActions();
  }
}

async function onApplyBatchDecision() {
  if (!state.projectId) {
    alert("请先选择项目");
    return;
  }
  const payload = {
    final_decision: el.batchDecision.value,
    reason_template: el.batchReasonTemplate.value.trim(),
    section_code_prefix: el.batchFilterSection.value.trim() || null,
    keyword: el.batchFilterKeyword.value.trim() || null,
    document_id: state.currentSorDocumentId || null,
    only_without_final: Boolean(el.batchOnlyEmpty.checked),
  };
  const result = await api(`/api/projects/${state.projectId}/requirements/batch-decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  el.batchResult.textContent = `批量更新完成：${result.updated_count} 条。`;
  await loadWorkspace();
}

async function loadProjects() {
  const projects = await api("/api/projects");
  await renderProjectOptions(projects);
}

async function renderProjectOptions(projects) {
  const current = state.projectId;
  el.projectSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = projects.length ? "请选择项目" : "暂无项目，请先创建";
  el.projectSelect.appendChild(placeholder);

  projects.forEach((project) => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name} / ${project.customer_name}`;
    if (project.id === current) {
      option.selected = true;
    }
    el.projectSelect.appendChild(option);
  });

  if (!current && projects.length) {
    state.projectId = projects[0].id;
    el.projectSelect.value = state.projectId;
    persistWorkspaceSelection();
    await onProjectChanged();
  }
}

async function loadSorDocuments(preferredDocumentId = null) {
  if (!state.projectId) {
    state.sorDocuments = [];
    state.currentSorDocumentId = null;
    renderSorDocumentOptions();
    return;
  }
  const docs = await api(`/api/projects/${state.projectId}/sor-documents`);
  state.sorDocuments = docs;
  if (preferredDocumentId && docs.some((doc) => doc.id === preferredDocumentId)) {
    state.currentSorDocumentId = preferredDocumentId;
  } else if (!state.currentSorDocumentId || !docs.some((doc) => doc.id === state.currentSorDocumentId)) {
    state.currentSorDocumentId = docs.length ? docs[0].id : null;
  }
  persistWorkspaceSelection();
  renderSorDocumentOptions();
}

function renderSorDocumentOptions() {
  if (!el.sorDocumentSelect) {
    return;
  }
  el.sorDocumentSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.sorDocuments.length ? "请选择 SOR 文档" : "当前项目暂无 SOR 文档";
  el.sorDocumentSelect.appendChild(placeholder);
  state.sorDocuments.forEach((doc) => {
    const option = document.createElement("option");
    option.value = doc.id;
    option.textContent = doc.file_name;
    if (doc.id === state.currentSorDocumentId) {
      option.selected = true;
    }
    el.sorDocumentSelect.appendChild(option);
  });
  el.sorDocumentSelect.value = state.currentSorDocumentId || "";
}

async function loadWorkspace() {
  stopReviewPreviewPolling();
  if (!state.projectId || !state.currentSorDocumentId) {
    state.workspace = null;
    state.reviewPreview = null;
    state.requirements = [];
    state.reviewEntries = [];
    state.reviewSelectedKind = "";
    state.reviewShowRecommendedSourceOnly = false;
    state.reviewActiveEntryId = "";
    state.reviewPdfDrawerOpen = false;
    initReviewPdfViewer();
    renderRequirements([]);
    renderWorkspaceSummary();
    renderReviewDiagnostics();
    renderReviewKindFilter();
    renderReviewEntries();
    setReviewStatus("");
    renderAiStage();
    syncWorkspaceActions();
    return;
  }
  const workspace = await api(`/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/workspace`);
  state.workspace = workspace;
  state.requirements = Array.isArray(workspace.requirements) ? workspace.requirements : [];
  state.reviewEntries = [];
  state.reviewSelectedKind = "";
  state.reviewShowRecommendedSourceOnly = false;
  state.reviewActiveEntryId = "";
  state.reviewPdfDrawerOpen = false;
  initReviewPdfViewer();
  renderRequirements(state.requirements);
  renderWorkspaceSummary();
  renderReviewDiagnostics();
  renderReviewKindFilter();
  renderReviewEntries();
  setReviewStatus("");
  renderAiStage();
  syncWorkspaceActions();
  if (state.currentStage === "review") {
    await loadReviewPreview();
  }
}

async function loadReviewPreview() {
  if (!state.projectId || !state.currentSorDocumentId) {
    state.reviewPreview = null;
    state.reviewEntries = [];
    state.reviewShowRecommendedSourceOnly = false;
    renderReviewDiagnostics();
    renderReviewKindFilter();
    renderReviewEntries();
    return;
  }
  state.reviewPreviewInFlight = true;
  syncWorkspaceActions();
  try {
    const previewPayload = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/extraction-preview`
    );
    state.reviewPreview = previewPayload;
    state.reviewEntries = Array.isArray(previewPayload.review_entries) ? previewPayload.review_entries : [];
    state.reviewShowRecommendedSourceOnly = false;
    renderReviewDiagnostics();
    renderReviewKindFilter();
    renderReviewEntries();
    syncWorkspacePreviewSummary(previewPayload);
    const status = String(previewPayload.preview_status || "").toUpperCase();
    if (status === "READY") {
      setReviewStatus(
        `预览已就绪，生成时间 ${formatTime(previewPayload.preview_generated_at) || "刚刚"}。`,
        "success"
      );
      stopReviewPreviewPolling();
    } else if (status === "BUILDING") {
      setReviewStatus("预览更新中，审阅条目会在后台刷新完成后自动恢复。", "warning");
      scheduleReviewPreviewPolling();
    } else if (status === "FAILED") {
      setReviewStatus(previewPayload.preview_error || "预览生成失败，请手动刷新重试。", "error");
      stopReviewPreviewPolling();
    } else {
      setReviewStatus("当前暂无预览缓存，正在后台生成预览。", "warning");
      await triggerReviewPreviewRefresh(false);
    }
  } finally {
    state.reviewPreviewInFlight = false;
    syncWorkspaceActions();
  }
}

async function onRefreshReviewPreview() {
  await triggerReviewPreviewRefresh(true);
}

async function triggerReviewPreviewRefresh(force) {
  if (!state.projectId || !state.currentSorDocumentId) {
    return;
  }
  const result = await api(
    `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/refresh-preview`,
    { method: "POST" }
  );
  syncWorkspacePreviewSummary(result);
  renderWorkspaceSummary();
  renderReviewDiagnostics();
  const accepted = Boolean(result.accepted);
  const status = String(result.preview_status || "").toUpperCase();
  if (accepted || status === "BUILDING") {
    setReviewStatus(force ? "预览刷新已启动，后台完成后会自动更新。" : "预览后台生成中。", "warning");
    scheduleReviewPreviewPolling();
  } else if (status === "READY") {
    await loadReviewPreview();
  }
}

function scheduleReviewPreviewPolling() {
  stopReviewPreviewPolling();
  state.reviewPreviewPollHandle = window.setTimeout(async () => {
    state.reviewPreviewPollHandle = null;
    if (!state.projectId || !state.currentSorDocumentId) {
      return;
    }
    await loadReviewPreview();
  }, 3000);
}

function stopReviewPreviewPolling() {
  if (state.reviewPreviewPollHandle) {
    window.clearTimeout(state.reviewPreviewPollHandle);
    state.reviewPreviewPollHandle = null;
  }
}

function syncWorkspacePreviewSummary(payload) {
  if (!payload) {
    return;
  }
  state.workspace = {
    ...(state.workspace || {}),
    summary: {
      ...((state.workspace && state.workspace.summary) || {}),
      preview_status: payload.preview_status || state.workspace?.summary?.preview_status || "NOT_READY",
      preview_generated_at:
        payload.preview_generated_at || state.workspace?.summary?.preview_generated_at || "",
      preview_cache_hit:
        typeof payload.preview_cache_hit === "boolean"
          ? payload.preview_cache_hit
          : Boolean(state.workspace?.summary?.preview_cache_hit),
      preview_error: payload.preview_error || "",
    },
    document: {
      ...((state.workspace && state.workspace.document) || {}),
      ...(payload.document || {}),
    },
  };
}

async function loadExportJobs() {
  if (!state.projectId) {
    state.exportJobs = [];
    renderExportOptions([]);
    return;
  }
  const jobs = await api(`/api/projects/${state.projectId}/exports`);
  state.exportJobs = jobs;
  renderExportOptions(jobs);
}

function renderExportOptions(jobs) {
  const current = el.exportCompareSelect.value;
  el.exportCompareSelect.innerHTML = `<option value="">不做版本对比</option>`;
  jobs.forEach((job) => {
    const option = document.createElement("option");
    option.value = job.id;
    const label = (job.version_label || "").trim();
    const stamp = formatTime(job.created_at);
    option.textContent = label ? `${label} (${stamp})` : `${job.file_name} (${stamp})`;
    if (job.id === current) {
      option.selected = true;
    }
    el.exportCompareSelect.appendChild(option);
  });
}

function renderWorkspaceSummary() {
  if (!el.workspaceSummary) {
    return;
  }
  const workspace = state.workspace || {};
  const workspaceDoc = workspace.document || {};
  const workspaceProject = workspace.project || {};
  const workspaceSummary = workspace.summary || {};
  const currentDoc = state.sorDocuments.find((doc) => doc.id === state.currentSorDocumentId) || null;
  const total = state.requirements.length;
  const finalCount = state.requirements.filter((item) => Boolean(item.final_decision)).length;
  const hybridCount = state.requirements.filter((item) => String(item.entry_kind || "").toUpperCase() === "HYBRID").length;
  const cards = [
    ["当前文档", workspaceDoc.file_name || currentDoc?.file_name || "未选择"],
    ["工作流", formatWorkflowStage(workspaceDoc.workflow_stage)],
    ["条目集版本", String(workspaceDoc.requirement_set_version ?? 0)],
    ["AI 状态", formatAiStatus(workspaceDoc.ai_status, workspaceDoc.ai_stale)],
    ["预览状态", formatPreviewStatus(workspaceSummary.preview_status)],
    ["知识库版本", String(workspaceProject.knowledge_base_version ?? 0)],
    ["知识文档", String(workspaceSummary.knowledge_doc_count ?? 0)],
    ["矩阵条目", String(total)],
    ["已人工确认", String(finalCount)],
    ["HYBRID", String(hybridCount)],
  ];
  el.workspaceSummary.innerHTML = cards
    .map(
      ([label, value]) => `<article class="summary-card"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(
        value
      )}</div></article>`
    )
    .join("");
  if (el.workspaceStatus) {
    if (!currentDoc && !workspaceDoc.file_name) {
      el.workspaceStatus.textContent = "请选择项目和 SOR 文档后再进入工作流。";
      return;
    }
    const stage = formatWorkflowStage(workspaceDoc.workflow_stage);
    const aiStatus = formatAiStatus(workspaceDoc.ai_status, workspaceDoc.ai_stale);
    const locked = workspaceSummary.locked ? "已锁定" : "未锁定";
    const previewStatus = formatPreviewStatus(workspaceSummary.preview_status);
    el.workspaceStatus.textContent = `当前文档 ${workspaceDoc.file_name || currentDoc?.file_name || ""}，阶段 ${stage}，条目集版本 ${
      workspaceDoc.requirement_set_version ?? 0
    }，${locked}，AI ${aiStatus}，预览 ${previewStatus}。`;
  }
}

function syncStagePanels() {
  const stage = state.currentStage || "review";
  el.stageSwitcher?.querySelectorAll("[data-stage]").forEach((button) => {
    button.classList.toggle("is-active", button.getAttribute("data-stage") === stage);
  });
  el.reviewStagePanel?.classList.toggle("is-hidden", stage !== "review");
  el.aiStagePanel?.classList.toggle("is-hidden", stage !== "ai");
  el.matrixStagePanel?.classList.toggle("is-hidden", stage !== "matrix");
  el.matrixPanel?.classList.toggle("is-hidden", stage !== "matrix");
}

function renderReviewDiagnostics() {
  const preview = state.reviewPreview || {};
  const summary = preview.summary || {};
  const workspaceSummary = state.workspace?.summary || {};
  if (el.reviewDiagnosticCards) {
    const cards = [
      ["预览状态", formatPreviewStatus(workspaceSummary.preview_status)],
      ["缓存命中", workspaceSummary.preview_cache_hit ? "是" : "否"],
      ["融合模式", summary.merge_mode || "-"],
      ["完整条目", summary.complete_entries_count ?? 0],
      ["原始条目", summary.complete_entries_raw_count ?? 0],
      ["章节数", summary.sections_count ?? 0],
      ["一致性问题", summary.section_consistency_issues_total ?? 0],
      ["低置信页", summary.low_confidence_page_count ?? 0],
      ["顺序来源", workspaceSummary.order_source || "-"],
      ["顺序异常", workspaceSummary.order_mismatch_count ?? 0],
    ];
    el.reviewDiagnosticCards.innerHTML = cards
      .map(
        ([label, value]) =>
          `<article class="summary-card"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(
            String(value)
          )}</div></article>`
      )
      .join("");
  }
  if (el.reviewSectionOverviewBody) {
    const rows = buildReviewSectionOverviewRows(preview.sections || []);
    if (!rows.length) {
      el.reviewSectionOverviewBody.innerHTML = `<tr><td colspan="5">暂无章节数据</td></tr>`;
    } else {
      el.reviewSectionOverviewBody.innerHTML = rows
        .map(
          (row) => `<tr>
            <td>${escapeHtml(formatScope(row.appendix_scope))}</td>
            <td>${escapeHtml(row.section_code || "")}</td>
            <td>${escapeHtml(row.section_title || "")}</td>
            <td>${escapeHtml(`${row.source_page_start || ""}-${row.source_page_end || ""}`)}</td>
            <td>${escapeHtml(String(row.requirement_count || 0))}</td>
          </tr>`
        )
        .join("");
    }
  }
}

function buildReviewSectionOverviewRows(rows) {
  return (rows || [])
    .map((row) => ({
      appendix_scope: row.appendix_scope || "",
      section_code: row.section_code || "",
      section_title: row.section_title || "",
      source_page_start: row.source_page_start || "",
      source_page_end: row.source_page_end || "",
      requirement_count: row.requirement_count || 0,
    }))
    .filter((row) => row.section_code || row.section_title);
}

function renderReviewKindFilter() {
  if (!el.reviewKindFilter) {
    return;
  }
  const kinds = new Set((state.reviewEntries || []).map((item) => String(item.entry_kind || "REQUIREMENT").toUpperCase()));
  const orderedKinds = ["HEADING", "HYBRID", "REQUIREMENT"].filter((kind) => kinds.has(kind));
  const labels = {
    HEADING: "HEADING 章节",
    HYBRID: "HYBRID 合并",
    REQUIREMENT: "REQUIREMENT 需求",
  };
  el.reviewKindFilter.innerHTML = [
    `<option value="">全部</option>`,
    ...orderedKinds.map((kind) => {
      const selected = state.reviewSelectedKind === kind ? "selected" : "";
      return `<option value="${kind}" ${selected}>${escapeHtml(labels[kind] || kind)}</option>`;
    }),
  ].join("");
}

function renderReviewEntries() {
  if (!el.reviewTableBody) {
    return;
  }
  const rows = filterReviewEntries(state.reviewEntries);
  if (!rows.length) {
    const previewStatus = String(state.workspace?.summary?.preview_status || "NOT_READY").toUpperCase();
    const message =
      previewStatus === "BUILDING"
        ? "预览生成中，完成后会自动显示审阅条目"
        : previewStatus === "FAILED"
        ? "预览生成失败，请点击“刷新预览”重试"
        : "暂无可审阅条目";
    el.reviewTableBody.innerHTML = `<tr><td colspan="5">${escapeHtml(message)}</td></tr>`;
    return;
  }
  el.reviewTableBody.innerHTML = rows
    .map((row, index) => {
      const keep = row.keep ? "checked" : "";
      const pageStart = Number(row.source_page || 0) || 0;
      const pageEnd = Number(row.source_page_end || pageStart) || pageStart;
      const span = pageStart > 0 ? `${pageStart}-${pageEnd || pageStart}` : "-";
      const entryId = escapeHtml(getReviewEntryKey(row) || `entry-${index}`);
      const activeClass = state.reviewActiveEntryId === getReviewEntryKey(row) ? "is-active" : "";
      const summary = renderTableDecisionSummary(row);
      return `<tr class="${activeClass}" data-review-entry-id="${entryId}" data-pdf-page="${pageStart}">
        <td><input type="checkbox" data-review-keep="${entryId}" ${keep} /></td>
        <td>
          <select data-review-kind-select="${entryId}">
            <option value="HEADING" ${String(row.entry_kind).toUpperCase() === "HEADING" ? "selected" : ""}>HEADING</option>
            <option value="REQUIREMENT" ${String(row.entry_kind).toUpperCase() === "REQUIREMENT" ? "selected" : ""}>REQUIREMENT</option>
            <option value="HYBRID" ${String(row.entry_kind).toUpperCase() === "HYBRID" ? "selected" : ""}>HYBRID</option>
          </select>
        </td>
        <td class="parent-section">${renderParentSectionLabel(row)}</td>
        <td class="req-text">${summary}${renderRequirementContent(row)}</td>
        <td>${escapeHtml(span)}</td>
      </tr>`;
    })
    .join("");
  syncReviewPdfDrawer();
  syncReviewSelectionButtons();
}

function filterReviewEntries(rows) {
  const sectionFilter = String(el.reviewSectionFilter?.value || "").trim().toLowerCase();
  const keyword = String(el.reviewKeywordFilter?.value || "").trim().toLowerCase();
  return (rows || []).filter((row) => {
    const entryKind = String(row.entry_kind || "REQUIREMENT").toUpperCase();
    if (state.reviewSelectedKind && state.reviewSelectedKind !== entryKind) {
      return false;
    }
    const sectionKey = [row.parent_section_label, row.section_code, row.section_title].filter(Boolean).join(" ").toLowerCase();
    if (sectionFilter && !sectionKey.includes(sectionFilter)) {
      return false;
    }
    const searchText = [row.raw_text, row.display_text, row.requirement_text, row.heading_text].join("\n").toLowerCase();
    if (keyword && !searchText.includes(keyword)) {
      return false;
    }
    if (state.reviewShowRecommendedSourceOnly && !rowHasRecommendedSource(row)) {
      return false;
    }
    return true;
  });
}

function rowHasRecommendedSource(row) {
  return (Array.isArray(row?.table_refs) ? row.table_refs : []).some(
    (ref) => String(ref?.recommended_export_variant || "").trim().toLowerCase() === "source"
  );
}

function renderTableDecisionSummary(row) {
  const refs = Array.isArray(row?.table_refs) ? row.table_refs : [];
  if (!refs.length) {
    return "";
  }
  const recommendedSource = refs.filter(
    (ref) => String(ref?.recommended_export_variant || "").trim().toLowerCase() === "source"
  ).length;
  const selectedSource = refs.filter((ref) => String(ref?.export_variant || "").trim().toLowerCase() === "source").length;
  const reasons = Array.from(
    new Set(
      refs
        .flatMap((ref) => (Array.isArray(ref?.export_variant_reasons) ? ref.export_variant_reasons : []))
        .map((item) => String(item || "").trim())
        .filter(Boolean)
    )
  ).slice(0, 3);
  const chips = [
    `<span class="table-export-badge">${
      recommendedSource > 0 ? `推荐截图 ${recommendedSource}/${refs.length}` : `默认表格 ${refs.length}/${refs.length}`
    }</span>`,
    `<span class="table-export-chip">导出截图 ${selectedSource}/${refs.length}</span>`,
    ...reasons.map((reason) => `<span class="table-export-chip is-reason">${escapeHtml(reason)}</span>`),
  ];
  return `<div class="table-export-summary">${chips.join("")}</div>`;
}

function toggleReviewRecommendedSourceOnly() {
  state.reviewShowRecommendedSourceOnly = !state.reviewShowRecommendedSourceOnly;
  syncReviewSelectionButtons();
  renderReviewEntries();
}

function syncReviewSelectionButtons() {
  const hasRows = filterReviewEntries(state.reviewEntries).length > 0;
  const disabled = state.reviewBulkApplyInFlight || state.reviewPreviewInFlight || !hasRows;
  syncReviewVisibleKeepToggle();
  if (el.reviewSelectAllBtn) {
    el.reviewSelectAllBtn.disabled = disabled;
  }
  if (el.reviewUnselectAllBtn) {
    el.reviewUnselectAllBtn.disabled = disabled;
  }
  if (el.reviewSourceRecommendedBtn) {
    el.reviewSourceRecommendedBtn.textContent = state.reviewShowRecommendedSourceOnly ? "显示全部条目" : "仅看推荐截图";
    el.reviewSourceRecommendedBtn.disabled = state.reviewBulkApplyInFlight || state.reviewPreviewInFlight || !hasRows;
  }
}

function syncReviewVisibleKeepToggle() {
  if (!el.reviewVisibleKeepToggle) {
    return;
  }
  const visibleRows = filterReviewEntries(state.reviewEntries);
  const disabled = state.reviewBulkApplyInFlight || state.reviewPreviewInFlight || !visibleRows.length;
  const keptCount = visibleRows.filter((row) => Boolean(row?.keep)).length;
  el.reviewVisibleKeepToggle.disabled = disabled;
  el.reviewVisibleKeepToggle.checked = Boolean(visibleRows.length) && keptCount === visibleRows.length;
  el.reviewVisibleKeepToggle.indeterminate = keptCount > 0 && keptCount < visibleRows.length;
}

function setActiveReviewRow(entryId) {
  document.querySelectorAll("#review-table-body tr[data-review-entry-id]").forEach((rowEl) => {
    const isActive = String(rowEl.getAttribute("data-review-entry-id") || "").trim() === String(entryId || "").trim();
    rowEl.classList.toggle("is-active", isActive);
  });
}

function buildReviewedExtractionEntriesPayload(rows) {
  return (rows || []).map((row) => ({
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
    dedupe_version: row.dedupe_version || state.reviewPreview?.summary?.dedupe_version || "",
    table_refs: Array.isArray(row.table_refs) ? row.table_refs : [],
    content_blocks: Array.isArray(row.content_blocks) ? row.content_blocks : [],
    has_table_asset: Boolean(row.has_table_asset),
    table_render_mode: row.table_render_mode || "",
    keep: Boolean(row.keep),
  }));
}

function applyWorkspacePayload(workspace) {
  state.workspace = workspace || null;
  state.reviewPreview = workspace?.preview || state.reviewPreview || null;
  state.requirements = Array.isArray(workspace?.requirements) ? workspace.requirements : [];
  state.reviewEntries = Array.isArray(workspace?.review_entries) ? workspace.review_entries : [];
  renderRequirements(state.requirements);
  renderWorkspaceSummary();
  renderReviewDiagnostics();
  renderReviewKindFilter();
  renderReviewEntries();
  renderAiStage();
  syncWorkspaceActions();
}

async function bulkSetReviewKeep(nextKeep) {
  if (!state.projectId || !state.currentSorDocumentId) {
    alert("请先选择项目和 SOR 文档");
    return;
  }
  const visibleRows = filterReviewEntries(state.reviewEntries);
  const changedRows = visibleRows.filter((row) => Boolean(row?.keep) !== Boolean(nextKeep));
  if (!changedRows.length) {
    setReviewStatus(nextKeep ? "当前筛选范围已经全部保留。" : "当前筛选范围已经全部移除。");
    return;
  }

  const previousKeepByEntryId = new Map(
    (state.reviewEntries || []).map((row) => [getReviewEntryKey(row), Boolean(row?.keep)])
  );
  changedRows.forEach((row) => {
    row.keep = Boolean(nextKeep);
  });
  const wasLocked = Boolean(state.workspace?.summary?.locked);
  state.reviewBulkApplyInFlight = true;
  renderReviewEntries();

  try {
    await api(`/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/apply-reviewed-extraction`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries: buildReviewedExtractionEntriesPayload(state.reviewEntries) }),
    });
    const workspace = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/workspace?include_preview=1`
    );
    applyWorkspacePayload(workspace);
    if (wasLocked && !Boolean(workspace?.summary?.locked)) {
      const warning = "条目集已变更，锁定已自动失效，请重新锁定后再导出或运行 AI。";
      state.exportStatusMessage = warning;
      state.exportStatusTone = "warning";
      renderExportStatus();
      setAnalyzeStatus(warning, "warning");
      setReviewStatus(warning, "warning");
    } else {
      state.exportStatusMessage = "";
      state.exportStatusTone = "";
      renderExportStatus();
      setReviewStatus(
        `${nextKeep ? "全部保留" : "全部移除"}已保存：当前筛选范围 ${changedRows.length} 条。`,
        "success"
      );
    }
  } catch (error) {
    (state.reviewEntries || []).forEach((row) => {
      row.keep = previousKeepByEntryId.get(getReviewEntryKey(row)) ?? Boolean(row.keep);
    });
    renderReviewEntries();
    setReviewStatus(normalizeApiError(error, "批量更新失败，请稍后重试。"), "error");
  } finally {
    state.reviewBulkApplyInFlight = false;
    syncReviewSelectionButtons();
  }
}

async function onReviewVisibleKeepToggleChanged(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement) || target.type !== "checkbox") {
    return;
  }
  await bulkSetReviewKeep(Boolean(target.checked));
}

async function onReviewTableChanged(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) {
    return;
  }
  const rowEl = target.closest("tr[data-review-entry-id]");
  if (!(rowEl instanceof HTMLTableRowElement)) {
    return;
  }
  const entryId = String(rowEl.getAttribute("data-review-entry-id") || "").trim();
  const row = state.reviewEntries.find((item) => getReviewEntryKey(item) === entryId);
  if (!row) {
    return;
  }
  const previous = {
    keep: Boolean(row.keep),
    entry_kind: String(row.entry_kind || "REQUIREMENT").toUpperCase(),
  };
  if (target instanceof HTMLInputElement && target.type === "checkbox") {
    row.keep = Boolean(target.checked);
  }
  if (target instanceof HTMLSelectElement) {
    row.entry_kind = String(target.value || "REQUIREMENT").toUpperCase();
  }
  rowEl.classList.add("is-saving");
  try {
    await saveReviewEntry(row);
    rowEl.classList.remove("is-saving");
  } catch (error) {
    row.keep = previous.keep;
    row.entry_kind = previous.entry_kind;
    rowEl.classList.remove("is-saving");
    renderReviewEntries();
    setReviewStatus(normalizeApiError(error, "条目保存失败，请稍后重试。"), "error");
  }
}

async function onReviewTableClicked(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (await handleReviewTableViewToggle(target)) {
    event.preventDefault();
    return;
  }
  if (target.closest("input") || target.closest("select")) {
    return;
  }
  const row = target.closest("tr[data-review-entry-id]");
  if (!(row instanceof HTMLTableRowElement)) {
    return;
  }
  const entryId = String(row.getAttribute("data-review-entry-id") || "").trim();
  const page = Number(row.getAttribute("data-pdf-page") || 0);
  focusReviewPdf(entryId, page);
}

async function handleReviewTableViewToggle(target) {
  const button = target.closest("button[data-table-variant]");
  if (!(button instanceof HTMLButtonElement)) {
    return false;
  }
  const tableId = String(button.getAttribute("data-table-id") || "").trim();
  const nextVariant = String(button.getAttribute("data-table-variant") || "").trim() === "source" ? "source" : "slice";
  if (!tableId) {
    return true;
  }
  const affected = setReviewTableExportVariant(tableId, nextVariant);
  state.tableViewByTableId[tableId] = nextVariant;
  
  if (affected) {
    const ref = (Array.isArray(affected.table_refs) ? affected.table_refs : []).find(r => String(r.table_id) === tableId);
    if (ref) {
      document.querySelectorAll(`div.table-asset-switcher[data-table-id="${tableId}"]`).forEach(el => {
        el.outerHTML = renderTableRef(ref);
      });
    }
  }

  if (!affected) {
    return true;
  }
  saveReviewEntry(affected, { renderRequirements: false, renderReviewEntries: false }).catch((error) => {
    setReviewTableExportVariant(tableId, affected.previousVariant);
    renderReviewEntries();
    renderRequirements(state.requirements);
    setReviewStatus(normalizeApiError(error, "导出表格选择保存失败，请稍后重试。"), "error");
  });
  return true;
}

function setReviewTableExportVariant(tableId, nextVariant) {
  const normalized = nextVariant === "source" ? "source" : "slice";
  let affected = null;
  [state.reviewEntries, state.requirements].forEach((rows) => {
    (rows || []).forEach((row) => {
      let touched = false;
      (Array.isArray(row?.table_refs) ? row.table_refs : []).forEach((ref) => {
        if (String(ref?.table_id || "").trim() !== tableId) {
          return;
        }
        if (!affected) {
          affected = {
            ...row,
            previousVariant: String(ref.export_variant || "").trim().toLowerCase() || "slice",
          };
        }
        ref.export_variant = normalized;
        ref.export_variant_overridden =
          normalized !== String(ref.recommended_export_variant || "slice").trim().toLowerCase();
        touched = true;
      });
      if (touched && affected && String(row?.req_code || "") === String(affected.req_code || "")) {
        affected = { ...row, previousVariant: affected.previousVariant };
      }
    });
  });
  return affected;
}

function onRequirementTableClicked(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (handleTableViewToggle(target)) {
    event.preventDefault();
    renderRequirements(state.requirements);
  }
}

function handleTableViewToggle(target) {
  const button = target.closest("button[data-table-variant]");
  if (!(button instanceof HTMLButtonElement)) {
    return false;
  }
  const tableId = String(button.getAttribute("data-table-id") || "").trim();
  const variant = String(button.getAttribute("data-table-variant") || "").trim();
  if (!tableId) {
    return true;
  }
  state.tableViewByTableId[tableId] = variant === "source" ? "source" : "slice";
  
  let refFound = null;
  (state.requirements || []).forEach(row => {
    (Array.isArray(row.table_refs) ? row.table_refs : []).forEach(ref => {
      if (String(ref.table_id) === tableId) {
        refFound = ref;
      }
    });
  });
  
  if (refFound) {
    document.querySelectorAll(`div.table-asset-switcher[data-table-id="${tableId}"]`).forEach(el => {
      el.outerHTML = renderTableRef(refFound);
    });
  }
  
  return true;
}

async function saveReviewEntry(row, options = {}) {
  if (!state.projectId || !state.currentSorDocumentId) {
    return;
  }
  const shouldRenderRequirements = options.renderRequirements !== false;
  const shouldRenderReviewEntries = options.renderReviewEntries !== false;
  const wasLocked = Boolean(state.workspace?.summary?.locked);
  const result = await api(
    `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/review-entry`,
    {
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
        keep: Boolean(row.keep),
      }),
    }
  );
  state.workspace = {
    ...(state.workspace || {}),
    project: result.project,
    document: result.document,
    summary: result.summary,
    actions: result.actions,
  };
  state.requirements = Array.isArray(result.requirements) ? result.requirements : [];
  mergeSavedReviewEntry(result.saved_entry || null);
  if (shouldRenderRequirements) {
    renderRequirements(state.requirements);
  }
  renderWorkspaceSummary();
  renderAiStage();
  syncWorkspaceActions();
  if (shouldRenderReviewEntries) {
    renderReviewEntries();
  }
  if (wasLocked && !Boolean(result.summary?.locked)) {
    const warning = "条目集已变更，锁定已自动失效，请重新锁定后再导出或运行 AI。";
    state.exportStatusMessage = warning;
    state.exportStatusTone = "warning";
    setAnalyzeStatus(warning, "warning");
    setReviewStatus(warning, "warning");
  } else {
    setReviewStatus("条目审阅结果已保存。");
  }
}

function initReviewPdfViewer() {
  const fileName = String(state.workspace?.document?.file_name || "");
  const isPdf = fileName.toLowerCase().endsWith(".pdf");
  if (!isPdf || !state.projectId || !state.currentSorDocumentId) {
    state.reviewPdfBaseUrl = "";
    state.reviewPdfCurrentPage = 1;
    state.reviewPdfViewerReady = false;
    state.reviewPendingPdfCommand = null;
    if (el.reviewPdfFrame) {
      el.reviewPdfFrame.removeAttribute("src");
    }
    if (el.reviewPdfHint) {
      el.reviewPdfHint.textContent = "当前文档不是 PDF，无法显示对照。";
    }
    if (el.reviewOpenPdfBtn) {
      el.reviewOpenPdfBtn.disabled = true;
    }
    syncReviewPdfDrawer();
    return;
  }
  state.reviewPdfBaseUrl = `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/file`;
  state.reviewPdfCurrentPage = 1;
  state.reviewPdfViewerReady = false;
  state.reviewPendingPdfCommand = null;
  if (state.currentStage === "review" && !state.reviewPdfDrawerOpen) {
    state.reviewPdfDrawerOpen = true;
  }
  if (el.reviewPdfFrame) {
    el.reviewPdfFrame.src = buildReviewPdfViewerUrl(1);
  }
  if (el.reviewPdfHint) {
    el.reviewPdfHint.textContent = "点击左侧条目定位原文页。";
  }
  if (el.reviewOpenPdfBtn) {
    el.reviewOpenPdfBtn.disabled = false;
  }
  syncReviewPdfDrawer();
}

function buildReviewPdfViewerUrl(pageNo) {
  const encodedPdf = encodeURIComponent(state.reviewPdfBaseUrl || "");
  return `/assets/pdf-viewer.html?v=20260306-3&pdf=${encodedPdf}&page=${Number(pageNo || 1)}`;
}

function onReviewPdfViewerMessage(event) {
  if (!el.reviewPdfFrame || event.source !== el.reviewPdfFrame.contentWindow) {
    return;
  }
  const data = event.data || {};
  if (data.type === "pdf-viewer-ready") {
    state.reviewPdfViewerReady = true;
    if (state.reviewPendingPdfCommand) {
      sendReviewPdfViewerCommand(
        state.reviewPendingPdfCommand.page,
        state.reviewPendingPdfCommand.highlightText || ""
      );
      state.reviewPendingPdfCommand = null;
    }
    return;
  }
  if (data.type === "pdf-page-changed") {
    const nextPage = Number(data.page || 0);
    if (nextPage > 0) {
      state.reviewPdfCurrentPage = nextPage;
      if (el.reviewPdfHint) {
        el.reviewPdfHint.textContent = `正在对照第 ${nextPage} 页。`;
      }
    }
  }
}

function focusReviewPdf(entryId, pageNo) {
  if (!state.reviewPdfBaseUrl) {
    return;
  }
  state.reviewActiveEntryId = entryId;
  setReviewPdfDrawerOpen(true);
  setActiveReviewRow(entryId);
  const row = state.reviewEntries.find((item) => getReviewEntryKey(item) === entryId);
  const highlightText = buildReviewHighlightText(row);
  if (pageNo > 0) {
    loadReviewPdfPage(pageNo, highlightText);
  }
}

function loadReviewPdfPage(pageNo, highlightText = "") {
  const safePage = Number(pageNo || 1) > 0 ? Number(pageNo || 1) : 1;
  state.reviewPdfCurrentPage = safePage;
  if (!el.reviewPdfFrame) {
    return;
  }
  if (state.reviewPdfViewerReady && el.reviewPdfFrame.contentWindow) {
    sendReviewPdfViewerCommand(safePage, highlightText);
  } else {
    state.reviewPendingPdfCommand = { page: safePage, highlightText: String(highlightText || "") };
    if (!String(el.reviewPdfFrame.src || "").includes("/assets/pdf-viewer.html")) {
      el.reviewPdfFrame.src = buildReviewPdfViewerUrl(safePage);
    }
  }
  if (el.reviewPdfHint) {
    el.reviewPdfHint.textContent = `正在对照第 ${safePage} 页。`;
  }
}

function sendReviewPdfViewerCommand(pageNo, highlightText = "") {
  if (!el.reviewPdfFrame?.contentWindow) {
    return;
  }
  el.reviewPdfFrame.contentWindow.postMessage(
    {
      type: "jump-page",
      page: Number(pageNo || 1),
      highlightText: String(highlightText || ""),
    },
    "*"
  );
}

function openReviewPdfInNewWindow() {
  if (!state.reviewPdfBaseUrl) {
    return;
  }
  const pageNo = Number(state.reviewPdfCurrentPage || 1);
  window.open(`${state.reviewPdfBaseUrl}#page=${pageNo}&zoom=page-fit`, "_blank", "noopener,noreferrer");
}

function onReviewPdfToggleClicked() {
  setReviewPdfDrawerOpen(!state.reviewPdfDrawerOpen);
}

function setReviewPdfDrawerOpen(nextOpen) {
  const scrollTop = Number(el.reviewTableWrap?.scrollTop || 0);
  const scrollLeft = Number(el.reviewTableWrap?.scrollLeft || 0);
  state.reviewPdfDrawerOpen = Boolean(nextOpen) && Boolean(state.reviewPdfBaseUrl);
  syncReviewPdfDrawer();
  if (el.reviewTableWrap) {
    requestAnimationFrame(() => {
      el.reviewTableWrap.scrollTop = scrollTop;
      el.reviewTableWrap.scrollLeft = scrollLeft;
    });
  }
}

function syncReviewPdfDrawer() {
  const isOpen = Boolean(state.reviewPdfDrawerOpen && state.reviewPdfBaseUrl);
  el.reviewLayout?.classList.toggle("has-review-pdf-open", isOpen);
  el.reviewPdfDrawer?.classList.toggle("is-collapsed", !isOpen);
  el.reviewPdfDrawer?.classList.toggle("is-open", isOpen);
  if (el.reviewPdfToggle) {
    el.reviewPdfToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    el.reviewPdfToggle.textContent = isOpen ? "收起PDF" : "展开PDF";
  }
}

function buildReviewHighlightText(row) {
  const text = String(row?.raw_text || row?.requirement_text || row?.display_text || "").trim();
  if (!text) {
    return "";
  }
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 2)
    .join(" ")
    .slice(0, 220);
}

function renderAiStage() {
  if (el.aiStageChecklist) {
    const workspace = state.workspace || {};
    const document = workspace.document || {};
    const summary = workspace.summary || {};
    const knowledgeCount = Number(summary.knowledge_doc_count || 0);
    const checklist = [
      [Number(document.requirement_set_version || 0) > 0, "已生成并保存条目集"],
      [Boolean(summary.locked), "条目集已锁定"],
      [knowledgeCount > 0, `已上传知识文档（${knowledgeCount}）`],
      [String(document.ai_status || "").toUpperCase() === "READY" && !document.ai_stale, "AI 结果已就绪"],
    ];
    el.aiStageChecklist.innerHTML = checklist
      .map(
        ([ok, label]) =>
          `<li class="${ok ? "is-ok" : "is-pending"}">${ok ? "已满足" : "待完成"}：${escapeHtml(label)}</li>`
      )
      .join("");
  }
  if (el.aiStageStatus) {
    const document = state.workspace?.document || {};
    el.aiStageStatus.innerHTML = `
      <p>当前阶段：<strong>${escapeHtml(formatWorkflowStage(document.workflow_stage))}</strong></p>
      <p>AI 状态：<strong>${escapeHtml(formatAiStatus(document.ai_status, document.ai_stale))}</strong></p>
      <p>条目集版本：<strong>${escapeHtml(String(document.requirement_set_version ?? 0))}</strong></p>
    `;
  }
}

function syncWorkspaceActions() {
  const workspace = state.workspace || {};
  const actions = workspace.actions || {};
  const hasDoc = Boolean(state.projectId && state.currentSorDocumentId);
  const workflowStage = String(workspace.document?.workflow_stage || "").toUpperCase();
  const aiStatus = String(workspace.document?.ai_status || "NOT_RUN").toUpperCase();
  const aiStale = Boolean(workspace.document?.ai_stale);
  const previewStatus = String(workspace.summary?.preview_status || "NOT_READY").toUpperCase();

  if (el.analyzeBtn) {
    el.analyzeBtn.disabled = !hasDoc || state.currentStage !== "review";
  }
  if (el.lockExtractionBtn) {
    const canLock = hasDoc && Boolean(actions.can_lock_extraction) && workflowStage !== "EXTRACTION_LOCKED";
    el.lockExtractionBtn.disabled = !canLock || state.lockInFlight;
    el.lockExtractionBtn.textContent = state.lockInFlight ? "锁定中..." : "锁定条目集";
  }
  if (el.runAiBtn) {
    el.runAiBtn.disabled = !hasDoc || !Boolean(actions.can_run_ai_analysis) || aiStatus === "RUNNING";
    if (aiStale) {
      el.runAiBtn.textContent = "重新运行 AI";
    } else {
      el.runAiBtn.textContent = "运行 AI 分析";
    }
  }
  if (el.exportBtn) {
    const canExport = hasDoc && Boolean(actions.can_export);
    el.exportBtn.disabled = !canExport || state.exportInFlight;
    el.exportBtn.textContent = state.exportInFlight ? "导出中..." : "导出 Compliance Matrix";
    el.exportBtn.title = canExport ? "" : "导出前需要先锁定当前条目集";
  }
  if (el.reviewRefreshPreviewBtn) {
    el.reviewRefreshPreviewBtn.disabled = !hasDoc || state.reviewPreviewInFlight || previewStatus === "BUILDING";
    el.reviewRefreshPreviewBtn.textContent = state.reviewPreviewInFlight || previewStatus === "BUILDING" ? "预览生成中..." : "刷新预览";
  }
  syncReviewSelectionButtons();
  renderExportStatus();
}

function requiresAnalyzeResetConfirmation() {
  const document = state.workspace?.document || {};
  const requirementSetVersion = Number(document.requirement_set_version || 0);
  const isLocked = Boolean(document.extraction_locked_at);
  const aiStatus = String(document.ai_status || "NOT_RUN").toUpperCase();
  return requirementSetVersion > 0 && (isLocked || aiStatus !== "NOT_RUN" || Boolean(document.ai_stale));
}

function renderExportStatus() {
  if (!el.exportStatus) {
    return;
  }
  const document = state.workspace?.document || {};
  const summary = state.workspace?.summary || {};
  const actions = state.workspace?.actions || {};
  const hasDoc = Boolean(state.projectId && state.currentSorDocumentId);
  const hasRequirements = state.requirements.length > 0;
  let message = "";
  let tone = "";

  if (state.exportStatusMessage) {
    message = state.exportStatusMessage;
    tone = state.exportStatusTone || "info";
  } else if (!hasDoc) {
    message = "选择项目和 SOR 文档后，才可导出 Compliance Matrix。";
  } else if (Number(document.requirement_set_version || 0) <= 0 || !hasRequirements) {
    message = "请先生成条目集并锁定后再导出 Compliance Matrix。";
    tone = "error";
  } else if (!summary.locked || !Boolean(actions.can_export)) {
    message = "导出 Compliance Matrix 前，请先锁定当前条目集。";
    tone = "error";
  }

  el.exportStatus.textContent = message;
  el.exportStatus.classList.toggle("is-error", tone === "error");
  el.exportStatus.classList.toggle("is-warning", tone === "warning");
  el.exportStatus.classList.toggle("is-success", tone === "success");
}

function mergeSavedReviewEntry(savedEntry) {
  if (!savedEntry || typeof savedEntry !== "object") {
    return;
  }
  const key = getReviewEntryKey(savedEntry);
  mergeSavedEntryIntoRows(state.reviewEntries, savedEntry, key);
  const reqCode = String(savedEntry.req_code || "").trim();
  if (reqCode) {
    const requirementIndex = state.requirements.findIndex((item) => String(item?.req_code || "").trim() === reqCode);
    if (requirementIndex >= 0) {
      state.requirements[requirementIndex] = {
        ...state.requirements[requirementIndex],
        ...savedEntry,
      };
    }
  }
}

function getReviewEntryKey(row) {
  return String(row?.entry_id || row?.req_code || "").trim();
}

function mergeSavedEntryIntoRows(rows, savedEntry, key) {
  let index = key ? rows.findIndex((item) => getReviewEntryKey(item) === key) : -1;
  if (index < 0) {
    index = rows.findIndex(
      (item) =>
        String(item?.source_anchor || "").trim() === String(savedEntry.source_anchor || "").trim() &&
        String(item?.raw_text || item?.requirement_text || "").trim() ===
          String(savedEntry.raw_text || savedEntry.requirement_text || "").trim()
    );
  }
  if (index >= 0) {
    rows[index] = {
      ...rows[index],
      ...savedEntry,
    };
    return;
  }
  if (key) {
    rows.push({ ...savedEntry });
  }
}

function setAnalyzeStatus(message, tone = "") {
  if (!el.analyzeResult) {
    return;
  }
  el.analyzeResult.textContent = String(message || "").trim();
  el.analyzeResult.classList.toggle("is-error", tone === "error");
  el.analyzeResult.classList.toggle("is-warning", tone === "warning");
  el.analyzeResult.classList.toggle("is-success", tone === "success");
}

function setReviewStatus(message, tone = "") {
  if (!el.reviewStatus) {
    return;
  }
  el.reviewStatus.textContent = String(message || "").trim();
  el.reviewStatus.classList.toggle("is-error", tone === "error");
  el.reviewStatus.classList.toggle("is-warning", tone === "warning");
  el.reviewStatus.classList.toggle("is-success", tone === "success");
}

function getApiErrorStatus(error) {
  const match = String(error?.message || "").match(/请求失败\((\d+)\)/);
  return match ? Number(match[1]) : 0;
}

function normalizeApiError(error, fallbackMessage) {
  const raw = String(error?.message || "").trim();
  const jsonMatch = raw.match(/\{[\s\S]*\}$/);
  if (jsonMatch) {
    try {
      const payload = JSON.parse(jsonMatch[0]);
      const detail = String(payload?.detail || "").trim();
      if (detail) {
        return detail;
      }
    } catch (parseError) {
      // Ignore parse failures and fall back to raw text.
    }
  }
  return raw || fallbackMessage;
}

function onReviewKindFilterChanged() {
  state.reviewSelectedKind = String(el.reviewKindFilter?.value || "").trim().toUpperCase();
  renderReviewEntries();
}

function renderRequirements(items) {
  renderMatrixStatus();
  el.tableBody.innerHTML = "";
  if (!items.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="6">暂无需求数据，请上传并分析SOR文档。</td>`;
    el.tableBody.appendChild(row);
    return;
  }

  [...items]
    .sort((left, right) => Number(left.display_sort_index || 0) - Number(right.display_sort_index || 0))
    .forEach((item) => {
    const row = document.createElement("tr");
    const aiDecision = item.ai_decision || "NEED_CLARIFICATION";
    const finalDecision = item.final_decision || aiDecision;
    const entryKind = String(item.entry_kind || "REQUIREMENT").toUpperCase();
    const textMeta = [];
    if (entryKind === "HYBRID") {
      textMeta.push(`<span class="mini-chip mini-chip-hybrid">HYBRID</span>`);
    }
    if (entryKind === "HEADING") {
      textMeta.push(`<span class="mini-chip mini-chip-delta">HEADING</span>`);
    }
    row.innerHTML = `
      <td>${escapeHtml(item.display_req_code || item.req_code || "")}</td>
      <td class="parent-section">${renderParentSectionLabel(item)}</td>
      <td class="req-text">${
        textMeta.length ? `<div class="req-meta">${textMeta.join("")}</div>` : ""
      }${renderRequirementContent(item)}</td>
      <td class="decision">${decisionSelect(item.id, finalDecision)}</td>
      <td><textarea data-reason="${item.id}" placeholder="填写 Schaeffler Comments">${escapeHtml(
        item.engineer_reason || ""
      )}</textarea></td>
      <td><button data-save="${item.id}" type="button">保存</button></td>
    `;
    el.tableBody.appendChild(row);
  });

  el.tableBody.querySelectorAll("[data-save]").forEach((button) => {
    button.addEventListener("click", wrap(onSaveDecision));
  });
}

function renderMatrixStatus() {
  if (!el.matrixStatus) {
    return;
  }
  const workspace = state.workspace || {};
  const workspaceDoc = workspace.document || {};
  const workspaceSummary = workspace.summary || {};
  const hasRequirements = state.requirements.length > 0;
  const shouldWarn =
    hasRequirements &&
    Number(workspaceDoc.requirement_set_version || 0) > 0 &&
    !Boolean(workspaceSummary.locked);

  el.matrixStatus.classList.toggle("is-hidden", !shouldWarn);
  if (!shouldWarn) {
    el.matrixStatus.textContent = "";
    return;
  }
  el.matrixStatus.textContent = "未锁定条目集，非正式 Compliance Matrix。锁定后再作为正式版本导出。";
}

async function onSaveDecision(event) {
  const requirementId = event.currentTarget.getAttribute("data-save");
  const decisionEl = el.tableBody.querySelector(`[data-decision="${requirementId}"]`);
  const reasonEl = el.tableBody.querySelector(`[data-reason="${requirementId}"]`);
  const payload = {
    final_decision: decisionEl.value,
    engineer_reason: reasonEl.value.trim(),
  };
  await api(`/api/requirements/${requirementId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setAnalyzeStatus(`已保存 ${requirementId} 的人工判定。`, "success");
  await loadWorkspace();
}

function decisionSelect(requirementId, value) {
  const options = ["ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION"];
  const html = options
    .map((option) => {
      const selected = option === value ? "selected" : "";
      return `<option value="${option}" ${selected}>${option}</option>`;
    })
    .join("");
  return `<select data-decision="${requirementId}">${html}</select>`;
}

function decisionChip(decision) {
  const map = {
    ACCEPT: ["chip-accept", "ACCEPT"],
    REJECT: ["chip-reject", "REJECT"],
    DEVIATION_ACCEPT: ["chip-deviation", "DEVIATION"],
    NEED_CLARIFICATION: ["chip-clarify", "CLARIFY"],
  };
  const [cls, label] = map[decision] || map.NEED_CLARIFICATION;
  return `<span class="status-chip ${cls}">${label}</span>`;
}

function renderParentSectionLabel(item) {
  const label = getParentSectionLabel(item);
  return escapeHtml(label || "-");
}

function getParentSectionLabel(item) {
  return (
    buildSectionLabel(item.parent_section_label, item.parent_section_code, item.parent_section_title) ||
    buildSectionLabel(item.section_label, item.section_code, item.section_title) ||
    extractSectionLabelFromAnchor(item.source_anchor) ||
    String(item.heading_text || "").trim()
  );
}

function buildSectionLabel(label, code, title) {
  const directLabel = String(label || "").trim();
  if (directLabel) {
    return directLabel;
  }
  return [String(code || "").trim(), String(title || "").trim()].filter(Boolean).join(" ");
}

function formatScope(scope) {
  const normalized = String(scope || "").trim().toUpperCase();
  return normalized || "Main";
}

function extractSectionLabelFromAnchor(anchor) {
  const source = String(anchor || "");
  const match = source.match(/(?:^|\|\s*)Section\s+([^|]+)/i);
  return match?.[1]?.trim() || "";
}

function renderRichRequirementText(text) {
  const source = String(text || "").replaceAll("\r\n", "\n");
  if (!source.trim()) {
    return "";
  }
  const lines = source.split("\n");
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
      while (i < lines.length && looksLikeMarkdownTableRow(lines[i])) {
        tableLines.push(lines[i]);
        i += 1;
      }
      i -= 1;
      blocks.push(renderMarkdownTable(tableLines));
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
  return `
    <div class="table-asset-switcher" data-table-id="${escapeHtml(tableId)}">
      <div class="table-view-toolbar">
        <span class="table-view-label">查看内容</span>
        <button type="button" class="table-view-btn${variant === "slice" ? " is-active" : ""}" data-table-id="${escapeHtml(
          tableId
        )}" data-table-variant="slice">表格</button>
        <button type="button" class="table-view-btn${variant === "source" ? " is-active" : ""}" data-table-id="${escapeHtml(
          tableId
        )}" data-table-variant="source">原文截图</button>
      </div>
      ${content}
    </div>
  `;
}

function activeTableVariant(ref) {
  const tableId = String(ref?.table_id || "").trim();
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
    if (ref?.source_snapshot_url) {
      return "source";
    }
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
  if (!text) {
    return false;
  }
  if (!text.includes("|")) {
    return false;
  }
  const parts = parseMarkdownCells(text);
  return parts.length >= 2;
}

function parseMarkdownCells(line) {
  let text = String(line || "");
  if (!text.trim()) {
    return [];
  }
  text = text.trim();
  if (text.startsWith("|")) {
    text = text.slice(1);
  }
  if (text.endsWith("|")) {
    text = text.slice(0, -1);
  }
  const placeholder = "__ESCAPED_PIPE__";
  return text
    .replaceAll("\\|", placeholder)
    .split("|")
    .map((cell) => cell.replaceAll(placeholder, "|").trim());
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

  const colgroup = buildMarkdownColgroup(head, body, maxCols);

  const thead = head.length
    ? `<thead><tr>${head.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("")}</tr></thead>`
    : "";
  const tbody = `<tbody>${body
    .map((cells) => `<tr>${cells.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;
  return `<div class="md-table-wrap"><table class="md-table">${colgroup}${thead}${tbody}</table></div>`;
}

function buildMarkdownColgroup(head, body, colCount) {
  const percents = computeMarkdownTableColumnPercents(head, body, colCount);
  if (!percents.length) {
    return "";
  }
  return `<colgroup>${percents.map((value) => `<col style="width:${value.toFixed(2)}%">`).join("")}</colgroup>`;
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

    return Math.max(3.8, Math.min(80, weight));
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
  let score = 0;
  for (const ch of String(text || "")) {
    score += /[\u4e00-\u9fff]/.test(ch) ? 2 : 1;
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

function rebalanceContentColumns(percents, labels, minPct) {
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

function isPrimaryDetailColumn(label) {
  const value = String(label || "").trim().toLowerCase();
  return /(试验方法|测试方法|方法|procedure|method|要求|需求|criteria|requirement)/i.test(value);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
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

function formatPreviewStatus(value) {
  const token = String(value || "").trim().toUpperCase() || "NOT_READY";
  const labels = {
    NOT_READY: "未生成",
    BUILDING: "生成中",
    READY: "已就绪",
    FAILED: "失败",
    STALE: "待刷新",
  };
  return labels[token] || token;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`请求失败(${response.status}): ${text}`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

function wrap(handler) {
  return async (event) => {
    try {
      await handler(event);
    } catch (error) {
      console.error(error);
      alert(error.message || "操作失败，请查看控制台日志。");
    }
  };
}
