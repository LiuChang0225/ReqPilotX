const state = {
  projectId: null,
  sorDocuments: [],
  currentSorDocumentId: null,
  currentStage: "review",
  workspace: null,
  knowledgeDocuments: [],
  aiAnalysisJob: null,
  aiAnalysisEvents: [],
  aiAnalysisPollHandle: null,
  aiFollowCurrentRequirement: true,
  aiSelectedRequirementId: "",
  aiSelectedProfileId: "",
  aiProviderSetup: null,
  aiRequirementDetailCache: {},
  aiRequirementDetailLoadingId: "",
  reviewPreview: null,
  requirements: [],
  reviewEntries: [],
  reviewSelectedKinds: [],
  reviewShowRecommendedSourceOnly: false,
  reviewDefaultTableVariant: "",
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
  reviewRenderDebounceHandle: null,
  knowledgeSyncRetryingId: "",
  knowledgeDeletingId: "",
};
const WORKSPACE_SELECTION_KEY = "reqpilotx.workspace-selection-v1";
const AI_TIMELINE_RENDER_LIMIT = 18;
const AI_LIVE_PREVIEW_LIMIT = 2400;
const AI_TIMELINE_PREVIEW_LIMIT = 560;
const AI_STAGE_ORDER = ["screening", "retrieval", "assessment"];
const AI_STREAM_EVENT_TYPES = new Set([
  "requirement_reasoning",
  "requirement_output",
  "requirement_answer_started",
  "requirement_screening_reasoning",
  "requirement_screening_output",
]);

const el = {
  projectForm: document.querySelector("#project-form"),
  projectName: document.querySelector("#project-name"),
  customerName: document.querySelector("#customer-name"),
  projectSelect: document.querySelector("#project-select"),
  sorDocumentSelect: document.querySelector("#sor-document-select"),
  removeSorBtn: document.querySelector("#remove-sor-btn"),
  refreshProjects: document.querySelector("#refresh-projects"),
  sorForm: document.querySelector("#sor-upload-form"),
  sorFile: document.querySelector("#sor-file"),
  sorUploadBtn: document.querySelector("#sor-upload-btn"),
  sorUploadProgress: document.querySelector("#sor-upload-progress"),
  sorUploadProgressFill: document.querySelector("#sor-upload-progress .upload-progress-fill"),
  sorUploadProgressText: document.querySelector("#sor-upload-progress .upload-progress-text"),
  sorUploadResult: document.querySelector("#sor-upload-result"),
  kbForm: document.querySelector("#kb-upload-form"),
  kbFile: document.querySelector("#kb-file"),
  kbUploadBtn: document.querySelector("#kb-upload-btn"),
  kbUploadProgress: document.querySelector("#kb-upload-progress"),
  kbUploadProgressFill: document.querySelector("#kb-upload-progress .upload-progress-fill"),
  kbUploadProgressText: document.querySelector("#kb-upload-progress .upload-progress-text"),
  kbDocType: document.querySelector("#kb-doc-type"),
  kbDocVersion: document.querySelector("#kb-doc-version"),
  kbDocStatus: document.querySelector("#kb-doc-status"),
  kbEffectiveFrom: document.querySelector("#kb-effective-from"),
  kbEffectiveTo: document.querySelector("#kb-effective-to"),
  kbUploadResult: document.querySelector("#kb-upload-result"),
  providerSetupForm: document.querySelector("#provider-setup-form"),
  providerSetupSaveBtn: document.querySelector("#provider-setup-save-btn"),
  providerSetupResult: document.querySelector("#provider-setup-result"),
  setupDefaultProfileSelect: document.querySelector("#setup-default-profile-select"),
  setupProfileHint: document.querySelector("#setup-profile-hint"),
  setupAzureFields: document.querySelector("#setup-azure-fields"),
  setupAzureAllowInteractive: document.querySelector("#setup-azure-allow-interactive"),
  setupAzureProbeBtn: document.querySelector("#setup-azure-probe-btn"),
  setupAzureProbeResult: document.querySelector("#setup-azure-probe-result"),
  setupAliyunFields: document.querySelector("#setup-aliyun-fields"),
  setupAliyunApiKey: document.querySelector("#setup-aliyun-api-key"),
  setupAliyunChatModel: document.querySelector("#setup-aliyun-chat-model"),
  setupAliyunEmbeddingModel: document.querySelector("#setup-aliyun-embedding-model"),
  setupAliyunBaseUrl: document.querySelector("#setup-aliyun-base-url"),
  setupAliyunQdrantUrl: document.querySelector("#setup-aliyun-qdrant-url"),
  setupAliyunQdrantCollection: document.querySelector("#setup-aliyun-qdrant-collection"),
  setupAliyunQdrantApiKey: document.querySelector("#setup-aliyun-qdrant-api-key"),
  knowledgeDocsSummary: document.querySelector("#knowledge-docs-summary"),
  knowledgeDocsBody: document.querySelector("#knowledge-docs-body"),
  analyzeBtn: document.querySelector("#analyze-btn"),
  lockExtractionBtn: document.querySelector("#lock-extraction-btn"),
  runAiBtn: document.querySelector("#run-ai-btn"),
  stopAiBtn: document.querySelector("#stop-ai-btn"),
  aiProfileSelect: document.querySelector("#ai-profile-select"),
  aiProfileHint: document.querySelector("#ai-profile-hint"),
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
  aiJobSummary: document.querySelector("#ai-job-summary"),
  aiProgressCurrent: document.querySelector("#ai-progress-current"),
  aiLiveReasoning: document.querySelector("#ai-live-reasoning"),
  aiProgressTimeline: document.querySelector("#ai-progress-timeline"),
  aiRequirementList: document.querySelector("#ai-requirement-list"),
  aiRequirementDetail: document.querySelector("#ai-requirement-detail"),
  aiDetailSummary: document.querySelector("#ai-detail-summary"),
  aiFollowCurrentToggle: document.querySelector("#ai-follow-current-toggle"),
};

init().catch((error) => {
  console.error(error);
  alert(error.message || "初始化失败，请检查后端是否已启动。");
});

async function init() {
  restoreWorkspaceSelection();
  bindEvents();
  syncStageFromUrl();
  restoreReviewFiltersFromUrl();
  syncStagePanels();
  await loadAiProviderSetup();
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
  const params = new URLSearchParams(window.location.search);
  const projectId = String(params.get("projectId") || "").trim();
  const documentId = String(params.get("documentId") || "").trim();
  if (projectId) {
    state.projectId = projectId;
  }
  if (documentId) {
    state.currentSorDocumentId = documentId;
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
  } else if (nextStage === "ai") {
    void ensureAiStageHydrated();
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

function restoreReviewFiltersFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const section = String(params.get("section") || "").trim();
  const keyword = String(params.get("keyword") || "").trim();
  const sourceOnly = String(params.get("sourceOnly") || "").trim().toLowerCase();
  const tableView = String(params.get("tableView") || "").trim().toLowerCase();
  if (el.reviewSectionFilter && section) {
    el.reviewSectionFilter.value = section;
  }
  if (el.reviewKeywordFilter && keyword) {
    el.reviewKeywordFilter.value = keyword;
  }
  if (sourceOnly) {
    state.reviewShowRecommendedSourceOnly = ["1", "true", "yes", "source"].includes(sourceOnly);
  }
  if (tableView === "slice" || tableView === "source") {
    state.reviewDefaultTableVariant = tableView;
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
  el.removeSorBtn?.addEventListener("click", wrap(onRemoveSorDocument));
  el.sorForm.addEventListener("submit", wrap(onUploadSor));
  el.kbForm.addEventListener("submit", wrap(onUploadKnowledge));
  el.providerSetupForm?.addEventListener("submit", wrap(onSaveAiProviderSetup));
  el.sorUploadBtn?.addEventListener("click", onSorUploadButtonClicked);
  el.kbUploadBtn?.addEventListener("click", onKnowledgeUploadButtonClicked);
  el.setupDefaultProfileSelect?.addEventListener("change", onAiProviderSetupSelectionChanged);
  el.setupAzureProbeBtn?.addEventListener("click", wrap(onProbeAzureRbac));
  el.analyzeBtn.addEventListener("click", wrap(onAnalyze));
  el.lockExtractionBtn.addEventListener("click", wrap(onLockExtraction));
  el.runAiBtn.addEventListener("click", wrap(onRunAiAnalysis));
  el.stopAiBtn?.addEventListener("click", wrap(onStopAiAnalysis));
  el.aiProfileSelect?.addEventListener("change", onAiProfileChanged);
  el.aiFollowCurrentToggle?.addEventListener("change", onAiFollowCurrentToggleChanged);
  el.reloadBtn.addEventListener("click", wrap(onReload));
  el.exportBtn.addEventListener("click", wrap(onExport));
  el.refreshExportsBtn.addEventListener("click", wrap(loadExportJobs));
  el.batchApplyBtn.addEventListener("click", wrap(onApplyBatchDecision));
  el.stageSwitcher?.addEventListener("click", onStageSwitcherClicked);
  el.reviewKindFilter?.addEventListener("click", onReviewKindFilterChanged);
  el.reviewSectionFilter?.addEventListener("input", scheduleReviewEntriesRender);
  el.reviewKeywordFilter?.addEventListener("input", scheduleReviewEntriesRender);
  el.reviewVisibleKeepToggle?.addEventListener("change", wrap(onReviewVisibleKeepToggleChanged));
  el.reviewSelectAllBtn?.addEventListener("click", wrap(() => bulkSetReviewKeep(true)));
  el.reviewUnselectAllBtn?.addEventListener("click", wrap(() => bulkSetReviewKeep(false)));
  el.reviewSourceRecommendedBtn?.addEventListener("click", toggleReviewRecommendedSourceOnly);
  el.reviewRefreshPreviewBtn?.addEventListener("click", wrap(onRefreshReviewPreview));
  el.knowledgeDocsBody?.addEventListener("click", wrap(onKnowledgeDocumentTableClicked));
  el.reviewTableBody?.addEventListener("change", wrap(onReviewTableChanged));
  el.reviewTableBody?.addEventListener("click", wrap(onReviewTableClicked));
  el.aiRequirementList?.addEventListener("click", wrap(onAiRequirementListClicked));
  el.aiLiveReasoning?.addEventListener("click", wrap(onAiLiveReasoningClicked));
  el.tableBody?.addEventListener("click", onRequirementTableClicked);
  el.reviewPdfToggle?.addEventListener("click", onReviewPdfToggleClicked);
  el.reviewOpenPdfBtn?.addEventListener("click", openReviewPdfInNewWindow);
  window.addEventListener("message", onReviewPdfViewerMessage);
}

function scheduleReviewEntriesRender() {
  if (state.reviewRenderDebounceHandle) {
    window.clearTimeout(state.reviewRenderDebounceHandle);
  }
  state.reviewRenderDebounceHandle = window.setTimeout(() => {
    state.reviewRenderDebounceHandle = null;
    renderReviewEntries();
  }, 140);
}

function requestUploadFromButton(form, fileInput) {
  if (!(form instanceof HTMLFormElement) || !(fileInput instanceof HTMLInputElement)) {
    return;
  }
  if (!fileInput.files?.length) {
    fileInput.click();
    return;
  }
  form.requestSubmit();
}

function onSorUploadButtonClicked() {
  requestUploadFromButton(el.sorForm, el.sorFile);
}

function onKnowledgeUploadButtonClicked() {
  requestUploadFromButton(el.kbForm, el.kbFile);
}

function stopAiAnalysisPolling() {
  if (state.aiAnalysisPollHandle) {
    window.clearTimeout(state.aiAnalysisPollHandle);
    state.aiAnalysisPollHandle = null;
  }
}

function shouldPollAiAnalysis() {
  const status = String(state.aiAnalysisJob?.status || "").trim().toUpperCase();
  return status === "PENDING" || status === "RUNNING" || status === "CANCEL_REQUESTED";
}

function scheduleAiAnalysisPolling(delayMs = 900) {
  stopAiAnalysisPolling();
  if (!shouldPollAiAnalysis() || !state.projectId || !state.currentSorDocumentId) {
    return;
  }
  state.aiAnalysisPollHandle = window.setTimeout(async () => {
    state.aiAnalysisPollHandle = null;
    await loadAiAnalysisStatus({ suppressErrors: true });
  }, delayMs);
}

function resetAiAnalysisState() {
  stopAiAnalysisPolling();
  state.aiAnalysisJob = null;
  state.aiAnalysisEvents = [];
  state.aiFollowCurrentRequirement = true;
  state.aiSelectedRequirementId = "";
  invalidateAiRequirementDetailCache();
}

function getCurrentAiRunningRequirementId() {
  const status = String(state.aiAnalysisJob?.status || "").trim().toUpperCase();
  if (!["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(status)) {
    return "";
  }
  return String(state.aiAnalysisJob?.current_requirement_id || "").trim();
}

function invalidateAiRequirementDetailCache() {
  state.aiRequirementDetailCache = {};
  state.aiRequirementDetailLoadingId = "";
}

function getAiInspectableRequirements() {
  const currentRequirementId = getCurrentAiRunningRequirementId();
  return [...(Array.isArray(state.requirements) ? state.requirements : [])]
    .filter((row) => {
      const requirementId = String(row?.id || "").trim();
      return Boolean(requirementId) && (
        requirementId === currentRequirementId ||
        Boolean(row?.ai_decision) ||
        Boolean(row?.analysis_basis) ||
        Boolean(row?.ai_reason) ||
        getAnalysisLabel(row) === "INFO" ||
        (Array.isArray(row?.evidences) && row.evidences.length > 0)
      );
    })
    .sort((left, right) => Number(left.display_sort_index || 0) - Number(right.display_sort_index || 0));
}

function ensureAiRequirementSelection() {
  const rows = getAiInspectableRequirements();
  const selectedId = String(state.aiSelectedRequirementId || "").trim();
  const currentRequirementId = getCurrentAiRunningRequirementId();
  if (state.aiFollowCurrentRequirement && currentRequirementId && rows.some((row) => String(row?.id || "") === currentRequirementId)) {
    state.aiSelectedRequirementId = currentRequirementId;
    return currentRequirementId;
  }
  if (selectedId && rows.some((row) => String(row?.id || "") === selectedId)) {
    return selectedId;
  }
  if (currentRequirementId && rows.some((row) => String(row?.id || "") === currentRequirementId)) {
    state.aiSelectedRequirementId = currentRequirementId;
    return currentRequirementId;
  }
  state.aiSelectedRequirementId = String(rows[0]?.id || "").trim();
  return state.aiSelectedRequirementId;
}

function syncAiFollowCurrentToggle() {
  if (!el.aiFollowCurrentToggle) {
    return;
  }
  el.aiFollowCurrentToggle.checked = Boolean(state.aiFollowCurrentRequirement);
  const currentRequirementId = getCurrentAiRunningRequirementId();
  el.aiFollowCurrentToggle.disabled = false;
  el.aiFollowCurrentToggle.title = currentRequirementId
    ? "开启后，右侧详情会自动跟随当前正在运行的条目。"
    : "当前没有正在运行的条目；下次 live 运行时会自动生效。";
}

function escapeSelectorValue(value) {
  const text = String(value || "");
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(text);
  }
  return text.replace(/(["\\])/g, "\\$1");
}

function syncAiRequirementViewports(selectedId, { resetDetail = false } = {}) {
  requestAnimationFrame(() => {
    if (resetDetail && el.aiRequirementDetail) {
      el.aiRequirementDetail.scrollTop = 0;
    }
    if (!el.aiRequirementList || !selectedId) {
      return;
    }
    const activeItem = el.aiRequirementList.querySelector(
      `[data-ai-requirement="${escapeSelectorValue(selectedId)}"]`
    );
    if (activeItem instanceof HTMLElement) {
      activeItem.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  });
}

function getAiProfiles() {
  return Array.isArray(state.workspace?.ai_profiles) ? state.workspace.ai_profiles : [];
}

function resolveDefaultAiProfileId() {
  const profiles = getAiProfiles();
  const explicitDefault = String(state.workspace?.document?.default_ai_profile_id || "").trim();
  if (explicitDefault && profiles.some((profile) => String(profile.id || "") === explicitDefault)) {
    return explicitDefault;
  }
  const flaggedDefault = profiles.find((profile) => Boolean(profile.default));
  if (flaggedDefault) {
    return String(flaggedDefault.id || "").trim();
  }
  return String(profiles[0]?.id || "").trim();
}

function getSelectedAiProfileId() {
  const profiles = getAiProfiles();
  const selected = String(state.aiSelectedProfileId || "").trim();
  if (selected && profiles.some((profile) => String(profile.id || "") === selected)) {
    return selected;
  }
  const fallback = resolveDefaultAiProfileId();
  state.aiSelectedProfileId = fallback;
  return fallback;
}

function getSelectedAiProfile() {
  const selectedId = getSelectedAiProfileId();
  return getAiProfiles().find((profile) => String(profile.id || "") === selectedId) || null;
}

function getAiProviderSetupProfiles() {
  return Array.isArray(state.aiProviderSetup?.profiles) ? state.aiProviderSetup.profiles : [];
}

function getConfiguredDefaultProfileId() {
  const explicit = String(state.aiProviderSetup?.default_profile_id || "").trim();
  if (explicit) {
    return explicit;
  }
  return resolveDefaultAiProfileId();
}

function getConfiguredDefaultProfile() {
  const defaultId = getConfiguredDefaultProfileId();
  return getAiProviderSetupProfiles().find((profile) => String(profile.id || "") === defaultId) || null;
}

function formatAiProfileOption(profile) {
  const label = String(profile.label || profile.id || "").trim() || "未命名路径";
  const mode = String(profile.assessment_mode || "").trim() || "heuristic";
  const retrieval = String(profile.retrieval_backend || "").trim() || "local";
  return `${label} · ${mode} / ${retrieval}`;
}

function formatAiProfileHint(profile) {
  if (!profile) {
    return "";
  }
  const runtimeDetail = formatAiProfileRuntimeDetail(profile);
  const sync = profile.knowledge_sync || {};
  const syncStatus = String(sync.status || "NOT_REQUIRED").trim().toUpperCase();
  const syncText =
    syncStatus === "READY"
      ? `知识同步 ${sync.ready_count || 0}/${sync.total_count || 0}`
      : syncStatus === "NOT_REQUIRED"
      ? "当前无需知识同步"
      : `知识同步 ${syncStatus}`;
  const missing = getAiProfileIssues(profile);
  if (profile.ready) {
    return `${profile.label || profile.id} 可运行，${syncText}。`;
  }
  if (runtimeDetail) {
    return `${profile.label || profile.id} 尚未就绪，${runtimeDetail}`;
  }
  if (missing.length) {
    return `${profile.label || profile.id} 尚未就绪，缺少：${missing.join(", ")}。`;
  }
  return `${profile.label || profile.id} 尚未就绪，${syncText}。`;
}

function getAiProfileIssues(profile) {
  const rawIssues = Array.isArray(profile?.runtime_missing) && profile.runtime_missing.length
    ? profile.runtime_missing
    : Array.isArray(profile?.missing)
    ? profile.missing
    : [];
  return rawIssues
    .map((item) => formatAiProfileIssue(String(item || "").trim()))
    .filter(Boolean);
}

function formatAiProfileIssue(issue) {
  const mapping = {
    REQPILOT_ALIYUN_API_KEY_NON_ASCII: "DashScope API Key 含有非 ASCII 字符",
    REQPILOT_ALIYUN_QDRANT_API_KEY_NON_ASCII: "Qdrant API Key 含有非 ASCII 字符",
    ALIYUN_QDRANT_UNREACHABLE: "本地 Qdrant 服务不可达",
    ALIYUN_RETRIEVAL_BACKEND_DISABLED: "Aliyun 检索后端尚未启用",
    DASHSCOPE_API_KEY: "DashScope API Key",
    REQPILOT_DASHSCOPE_BASE_URL: "DashScope Base URL",
    REQPILOT_ALIYUN_CHAT_MODEL: "Aliyun Chat 模型",
    REQPILOT_ALIYUN_EMBEDDING_MODEL: "Aliyun Embedding 模型",
    REQPILOT_ALIYUN_QDRANT_URL: "Qdrant URL",
    REQPILOT_ALIYUN_QDRANT_COLLECTION: "Qdrant Collection",
    AZURE_OPENAI_ENDPOINT: "Azure OpenAI Endpoint",
    AZURE_OPENAI_CHAT_DEPLOYMENT: "Azure OpenAI Chat Deployment",
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: "Azure OpenAI Embedding Deployment",
    AZURE_AI_SEARCH_ENDPOINT: "Azure AI Search Endpoint",
    AZURE_AI_SEARCH_INDEX_NAME: "Azure AI Search Index Name",
    AZURE_OPENAI_API_KEY: "Azure OpenAI API Key",
    AZURE_AI_SEARCH_API_KEY: "Azure AI Search API Key",
  };
  return mapping[issue] || issue;
}

function formatAiProfileRuntimeDetail(profile) {
  const detail = String(profile?.runtime_detail || "").trim();
  if (!detail) {
    return "";
  }
  const issues = getAiProfileIssues(profile);
  if (issues.includes("本地 Qdrant 服务不可达")) {
    return "本地 Qdrant 服务不可达，请先启动 `http://localhost:6333` 对应实例。";
  }
  return detail;
}

function renderAiProfileSelector() {
  const profiles = getAiProfiles();
  const selectedProfile = getSelectedAiProfile();
  const defaultProfile = getConfiguredDefaultProfile() || selectedProfile;
  if (!el.aiProfileSelect) {
    if (el.aiProfileHint) {
      if (!profiles.length) {
        el.aiProfileHint.textContent = "当前工作区还没有可渲染的 AI 路径配置。";
      } else if (defaultProfile) {
        el.aiProfileHint.textContent = `AI 分析会直接沿用第 2 步默认路径：${formatAiProfileHint(defaultProfile)}`;
      } else {
        el.aiProfileHint.textContent = "请先在第 2 步完成默认 AI 路径设置。";
      }
    }
    return;
  }
  if (!profiles.length) {
    el.aiProfileSelect.innerHTML = `<option value="">暂无可选 AI 路径</option>`;
    el.aiProfileSelect.disabled = true;
    if (el.aiProfileHint) {
      el.aiProfileHint.textContent = "当前工作区还没有可渲染的 AI 路径配置。";
    }
    return;
  }
  const selectedId = getSelectedAiProfileId();
  el.aiProfileSelect.innerHTML = profiles
    .map((profile) => {
      const profileId = String(profile.id || "").trim();
      const suffix = profile.ready ? "可用" : "未就绪";
      const selected = profileId === selectedId ? " selected" : "";
      return `<option value="${escapeHtml(profileId)}"${selected}>${escapeHtml(
        `${formatAiProfileOption(profile)} · ${suffix}`
      )}</option>`;
    })
    .join("");
  el.aiProfileSelect.disabled = false;
  if (el.aiProfileHint) {
    el.aiProfileHint.textContent = defaultProfile
      ? `AI 分析会直接沿用第 2 步默认路径：${formatAiProfileHint(defaultProfile)}`
      : formatAiProfileHint(selectedProfile);
  }
}

function onAiProfileChanged(event) {
  state.aiSelectedProfileId = String(event?.target?.value || "").trim();
  renderAiProfileSelector();
  renderAiStage();
  syncWorkspaceActions();
}

function renderAiProviderSetup(selectedProfileId = "") {
  if (!el.setupDefaultProfileSelect) {
    return;
  }
  const setup = state.aiProviderSetup || {};
  const defaultProfileId = String(selectedProfileId || "").trim() || getConfiguredDefaultProfileId() || "azure";
  const profiles = getAiProviderSetupProfiles();
  const defaultProfile =
    profiles.find((profile) => String(profile.id || "").trim() === defaultProfileId) || getConfiguredDefaultProfile();
  const azureSetup = setup.azure || {};
  const aliyunSetup = setup.aliyun || {};

  el.setupDefaultProfileSelect.value = defaultProfileId;
  if (el.setupProfileHint) {
    el.setupProfileHint.textContent = defaultProfile
      ? formatAiProfileHint(defaultProfile)
      : "请先选择默认 AI 路径，再上传知识库文档。";
  }
  if (el.setupAzureFields) {
    el.setupAzureFields.hidden = defaultProfileId !== "azure";
  }
  if (el.setupAliyunFields) {
    el.setupAliyunFields.hidden = defaultProfileId !== "aliyun";
  }
  if (el.setupAzureAllowInteractive) {
    el.setupAzureAllowInteractive.checked = Boolean(azureSetup.allow_interactive_login);
  }
  if (el.setupAliyunChatModel) {
    el.setupAliyunChatModel.value = aliyunSetup.chat_model || "qwen3.5-plus";
  }
  if (el.setupAliyunEmbeddingModel) {
    el.setupAliyunEmbeddingModel.value = aliyunSetup.embedding_model || "text-embedding-v4";
  }
  if (el.setupAliyunBaseUrl) {
    el.setupAliyunBaseUrl.value = aliyunSetup.base_url || "https://dashscope.aliyuncs.com/compatible-mode/v1";
  }
  if (el.setupAliyunQdrantUrl) {
    el.setupAliyunQdrantUrl.value = aliyunSetup.qdrant_url || "http://localhost:6333";
  }
  if (el.setupAliyunQdrantCollection) {
    el.setupAliyunQdrantCollection.value = aliyunSetup.qdrant_collection || "reqpilot-knowledge-aliyun-local";
  }
  if (el.setupAliyunApiKey) {
    el.setupAliyunApiKey.placeholder = aliyunSetup.api_key_configured ? "已配置，留空则保持不变" : "sk-...";
  }
  if (el.setupAliyunQdrantApiKey) {
    el.setupAliyunQdrantApiKey.placeholder = aliyunSetup.qdrant_api_key_configured ? "已配置，留空则保持不变" : "可选";
  }
}

function onAiProviderSetupSelectionChanged() {
  const selectedProfileId = String(el.setupDefaultProfileSelect?.value || "").trim() || "azure";
  renderAiProviderSetup(selectedProfileId);
  syncWorkspaceActions();
}

function applyAiProviderSetup(payload) {
  state.aiProviderSetup = payload || null;
  state.aiSelectedProfileId = getConfiguredDefaultProfileId();
  renderAiProviderSetup();
  renderAiProfileSelector();
  syncWorkspaceActions();
}

async function loadAiProviderSetup() {
  const payload = await api("/api/runtime/ai-provider-setup");
  applyAiProviderSetup(payload);
  return payload;
}

async function onSaveAiProviderSetup(event) {
  event.preventDefault();
  const defaultProfileId = String(el.setupDefaultProfileSelect?.value || "azure").trim() || "azure";
  const payload = {
    default_profile_id: defaultProfileId,
    azure: {
      auth_mode: "rbac",
      allow_interactive_login: Boolean(el.setupAzureAllowInteractive?.checked),
    },
    aliyun: {
      retrieval_backend: "qdrant",
      base_url: String(el.setupAliyunBaseUrl?.value || "").trim(),
      chat_model: String(el.setupAliyunChatModel?.value || "").trim(),
      embedding_model: String(el.setupAliyunEmbeddingModel?.value || "").trim(),
      qdrant_url: String(el.setupAliyunQdrantUrl?.value || "").trim(),
      qdrant_collection: String(el.setupAliyunQdrantCollection?.value || "").trim(),
    },
  };
  const apiKey = String(el.setupAliyunApiKey?.value || "").trim();
  if (apiKey && !isAsciiText(apiKey)) {
    if (el.providerSetupResult) {
      el.providerSetupResult.textContent = "DashScope API Key 含有非 ASCII 字符，请重新粘贴原始 key，不要带中文标点或全角字符。";
    }
    return;
  }
  if (apiKey) {
    payload.aliyun.api_key = apiKey;
  }
  const qdrantApiKey = String(el.setupAliyunQdrantApiKey?.value || "").trim();
  if (qdrantApiKey && !isAsciiText(qdrantApiKey)) {
    if (el.providerSetupResult) {
      el.providerSetupResult.textContent = "Qdrant API Key 含有非 ASCII 字符，请重新粘贴原始 key。";
    }
    return;
  }
  if (qdrantApiKey) {
    payload.aliyun.qdrant_api_key = qdrantApiKey;
  }
  if (el.providerSetupSaveBtn) {
    el.providerSetupSaveBtn.disabled = true;
  }
  if (el.providerSetupResult) {
    el.providerSetupResult.textContent = "AI 路径配置已提交，正在刷新状态...";
  }
  try {
    const result = await api("/api/runtime/ai-provider-setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applyAiProviderSetup(result);
    if (el.setupAliyunApiKey) {
      el.setupAliyunApiKey.value = "";
    }
    if (el.setupAliyunQdrantApiKey) {
      el.setupAliyunQdrantApiKey.value = "";
    }
    if (el.providerSetupResult) {
      el.providerSetupResult.textContent = `默认 AI 路径已切到 ${defaultProfileId === "aliyun" ? "Aliyun" : "Azure"}。`;
    }
    if (state.projectId && state.currentSorDocumentId) {
      await loadWorkspace({ skipAiStatus: true });
    }
  } finally {
    if (el.providerSetupSaveBtn) {
      el.providerSetupSaveBtn.disabled = false;
    }
    syncWorkspaceActions();
  }
}

async function onProbeAzureRbac() {
  if (el.setupAzureProbeBtn) {
    el.setupAzureProbeBtn.disabled = true;
  }
  if (el.setupAzureProbeResult) {
    el.setupAzureProbeResult.textContent = "正在检查 Azure RBAC，本地首次调用可能触发系统浏览器登录...";
  }
  try {
    const result = await api("/api/runtime/ai-provider-setup/azure-rbac-probe", { method: "POST" });
    const checks = Array.isArray(result?.checks) ? result.checks : [];
    const summary = checks
      .map((item) => {
        const label = item.name || item.scope || "scope";
        const status = String(item.status || "").toUpperCase();
        return status === "OK" ? `${label}: OK` : `${label}: ${item.detail || status}`;
      })
      .join("；");
    if (el.setupAzureProbeResult) {
      el.setupAzureProbeResult.textContent = result?.ok
        ? `Azure RBAC 检查通过。${summary}`
        : `Azure RBAC 仍未就绪。${summary}`;
    }
    await loadAiProviderSetup();
    if (state.projectId && state.currentSorDocumentId) {
      await loadWorkspace({ skipAiStatus: true });
    }
  } finally {
    if (el.setupAzureProbeBtn) {
      el.setupAzureProbeBtn.disabled = false;
    }
  }
}

async function ensureAiStageHydrated() {
  if (!state.projectId || !state.currentSorDocumentId) {
    return;
  }
  await loadAiAnalysisStatus({ suppressErrors: true, refreshWorkspaceOnTerminal: false });
  const selectedId = ensureAiRequirementSelection();
  renderAiStage();
  if (selectedId && !state.aiRequirementDetailCache[selectedId]) {
    void selectAiRequirement(selectedId, { silent: true });
  }
}

function applyAiAnalysisStatus(payload) {
  const previousSelectedId = String(state.aiSelectedRequirementId || "").trim();
  state.aiAnalysisJob = payload?.job || null;
  state.aiAnalysisEvents = Array.isArray(payload?.events) ? payload.events : [];
  if (payload?.job?.profile_id) {
    state.aiSelectedProfileId = String(payload.job.profile_id || "").trim();
  }
  if (payload?.document) {
    state.workspace = {
      ...(state.workspace || {}),
      document: {
        ...((state.workspace && state.workspace.document) || {}),
        ...payload.document,
      },
    };
  }
  if (Array.isArray(payload?.ai_profiles)) {
    state.workspace = {
      ...(state.workspace || {}),
      ai_profiles: payload.ai_profiles,
    };
  }
  const nextSelectedId = ensureAiRequirementSelection();
  syncAiFollowCurrentToggle();
  renderAiStage();
  syncWorkspaceActions();
  if (
    state.currentStage === "ai" &&
    nextSelectedId &&
    nextSelectedId !== previousSelectedId &&
    !state.aiRequirementDetailCache[nextSelectedId] &&
    state.aiRequirementDetailLoadingId !== nextSelectedId
  ) {
    void selectAiRequirement(nextSelectedId, { silent: true });
  }
  if (shouldPollAiAnalysis()) {
    scheduleAiAnalysisPolling();
  } else {
    stopAiAnalysisPolling();
  }
}

async function loadAiAnalysisStatus({ suppressErrors = false, refreshWorkspaceOnTerminal = true } = {}) {
  if (!state.projectId || !state.currentSorDocumentId) {
    return null;
  }
  const previousStatus = String(state.aiAnalysisJob?.status || "").trim().toUpperCase();
  try {
    const payload = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/ai-analysis-status?event_limit=120`
    );
    applyAiAnalysisStatus(payload);
    const nextStatus = String(payload?.job?.status || "").trim().toUpperCase();
    if (
      refreshWorkspaceOnTerminal &&
      previousStatus &&
      previousStatus !== nextStatus &&
      ["COMPLETED", "FAILED", "CANCELLED"].includes(nextStatus)
    ) {
      invalidateAiRequirementDetailCache();
      await loadWorkspace({ skipAiStatus: true });
    }
    return payload;
  } catch (error) {
    if (!suppressErrors) {
      setAnalyzeStatus(normalizeApiError(error, "AI 状态刷新失败，请稍后重试。"), "error");
    }
    return null;
  }
}

async function selectAiRequirement(requirementId, { force = false, silent = false } = {}) {
  const nextId = String(requirementId || "").trim();
  const previousId = String(state.aiSelectedRequirementId || "").trim();
  const selectionChanged = nextId !== previousId;
  state.aiSelectedRequirementId = nextId;
  if (!nextId) {
    renderAiStage();
    return null;
  }
  if (!force && state.aiRequirementDetailCache[nextId]) {
    renderAiStage();
    syncAiRequirementViewports(nextId, { resetDetail: selectionChanged });
    return state.aiRequirementDetailCache[nextId];
  }
  state.aiRequirementDetailLoadingId = nextId;
  renderAiStage();
  syncAiRequirementViewports(nextId, { resetDetail: selectionChanged });
  try {
    const detail = await api(`/api/requirements/${nextId}/analysis-detail`);
    state.aiRequirementDetailCache[nextId] = detail;
    return detail;
  } catch (error) {
    if (!silent) {
      setAnalyzeStatus(normalizeApiError(error, "AI 证据详情加载失败，请稍后重试。"), "error");
    }
    return null;
  } finally {
    if (state.aiRequirementDetailLoadingId === nextId) {
      state.aiRequirementDetailLoadingId = "";
    }
    renderAiStage();
  }
}

async function onAiRequirementListClicked(event) {
  const button = event.target.closest("[data-ai-requirement]");
  if (!(button instanceof HTMLButtonElement)) {
    return;
  }
  const requirementId = String(button.getAttribute("data-ai-requirement") || "").trim();
  if (!requirementId) {
    return;
  }
  const currentRequirementId = getCurrentAiRunningRequirementId();
  if (currentRequirementId && requirementId !== currentRequirementId) {
    state.aiFollowCurrentRequirement = false;
    syncAiFollowCurrentToggle();
  }
  await selectAiRequirement(requirementId);
}

function openAiRequirementDetail(requirementId) {
  const nextId = String(requirementId || "").trim();
  if (!nextId) {
    return;
  }
  const currentRequirementId = getCurrentAiRunningRequirementId();
  if (currentRequirementId && nextId !== currentRequirementId) {
    state.aiFollowCurrentRequirement = false;
    syncAiFollowCurrentToggle();
  }
  state.currentStage = "ai";
  syncStagePanels();
  syncStageUrl();
  syncWorkspaceActions();
  void selectAiRequirement(nextId);
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

async function onProjectChanged(preferredDocumentId = null) {
  const preferredDocument =
    typeof preferredDocumentId === "string" && preferredDocumentId.trim() ? preferredDocumentId.trim() : null;
  state.projectId = el.projectSelect.value || null;
  state.currentSorDocumentId = preferredDocument || null;
  state.sorDocuments = [];
  state.workspace = null;
  resetAiAnalysisState();
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
    await loadSorDocuments(state.currentSorDocumentId || null);
    await Promise.all([loadWorkspace(), loadExportJobs(), loadKnowledgeDocuments()]);
  } else {
    state.knowledgeDocuments = [];
    renderRequirements([]);
    renderKnowledgeDocuments();
    renderExportOptions([]);
    renderWorkspaceSummary();
    renderReviewDiagnostics();
    renderReviewEntries();
    syncWorkspaceActions();
  }
}

async function onSorDocumentChanged() {
  state.currentSorDocumentId = el.sorDocumentSelect.value || null;
  resetAiAnalysisState();
  state.reviewPreview = null;
  state.exportStatusMessage = "";
  state.exportStatusTone = "";
  state.tableViewByTableId = {};
  persistWorkspaceSelection();
  await loadWorkspace();
}

async function onRemoveSorDocument() {
  if (!state.projectId || !state.currentSorDocumentId) {
    alert("请先选择要移除的 SOR 文档");
    return;
  }
  const doc = state.sorDocuments.find((d) => d.id === state.currentSorDocumentId);
  const fileName = doc?.file_name || state.currentSorDocumentId;
  const accepted = window.confirm(`确认移除 SOR 文档「${fileName}」？\n\n该文档及其关联的条目集、AI 分析记录将被一并删除，此操作不可撤销。`);
  if (!accepted) return;
  el.removeSorBtn.disabled = true;
  try {
    await api(`/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}`, {
      method: "DELETE",
    });
    await loadSorDocuments();
    await loadWorkspace();
  } finally {
    el.removeSorBtn.disabled = false;
  }
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
  showUploadProgress("sor");
  try {
    const document = await uploadWithProgress(
      `/api/projects/${state.projectId}/sor-documents`,
      form,
      (percent) => {
        if (percent >= 100) {
          setUploadProcessing("sor", true);
        } else {
          setUploadProgress("sor", percent);
        }
      }
    );
    setUploadProcessing("sor", false);
    await loadSorDocuments(document.id);
    await loadWorkspace();
    el.sorUploadResult.textContent = `已上传: ${document.file_name}`;
    el.sorFile.value = "";
  } catch (err) {
    setUploadProcessing("sor", false);
    el.sorUploadResult.textContent = `上传失败: ${err.message}`;
    el.sorUploadResult.className = "hint is-error";
  } finally {
    hideUploadProgress("sor");
  }
}

async function onUploadKnowledge(event) {
  event.preventDefault();
  if (!state.projectId) {
    alert("请先选择项目");
    return;
  }
  const defaultKnowledgeProfile = getConfiguredDefaultProfile();
  if (!defaultKnowledgeProfile?.retrieval_ready) {
    alert(`请先完成默认 AI 路径配置。${formatAiProfileHint(defaultKnowledgeProfile)}`);
    return;
  }
  const file = el.kbFile.files?.[0];
  if (!file) {
    alert("请选择知识文档");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("doc_type", "REFERENCE");
  form.append("status", "ACTIVE");
  showUploadProgress("kb");
  try {
    const document = await uploadWithProgress(
      `/api/projects/${state.projectId}/knowledge-documents`,
      form,
      (percent) => {
        if (percent >= 100) {
          setUploadProcessing("kb", true);
        } else {
          setUploadProgress("kb", percent);
        }
      }
    );
    setUploadProcessing("kb", false);
    el.kbUploadResult.textContent = `已上传: ${document.file_name}，知识库版本 ${document.knowledge_base_version}`;
    el.kbFile.value = "";
    if (el.kbDocVersion) el.kbDocVersion.value = "";
    if (el.kbEffectiveFrom) el.kbEffectiveFrom.value = "";
    if (el.kbEffectiveTo) el.kbEffectiveTo.value = "";
    if (el.kbDocType) el.kbDocType.value = "REFERENCE";
    if (el.kbDocStatus) el.kbDocStatus.value = "ACTIVE";
    await loadKnowledgeDocuments();
    await loadWorkspace();
  } catch (err) {
    setUploadProcessing("kb", false);
    el.kbUploadResult.textContent = `上传失败: ${err.message}`;
    el.kbUploadResult.className = "hint is-error";
  } finally {
    hideUploadProgress("kb");
  }
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
    ensureAiRequirementSelection();
    renderRequirements(state.requirements);
    renderWorkspaceSummary();
    renderAiStage();
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
  stopAiAnalysisPolling();
  invalidateAiRequirementDetailCache();
  state.aiFollowCurrentRequirement = true;
  syncAiFollowCurrentToggle();
  el.runAiBtn.disabled = true;
  setAnalyzeStatus("AI 分析已启动，页面会持续显示阶段、证据链和最终结论。", "warning");
  try {
    const selectedProfileId = getConfiguredDefaultProfileId();
    const payload = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/run-ai-analysis/start`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: selectedProfileId || null }),
      }
    );
    applyAiAnalysisStatus(payload);
    state.currentStage = "ai";
    syncStagePanels();
    syncStageUrl();
    const selectedId = ensureAiRequirementSelection();
    if (selectedId) {
      void selectAiRequirement(selectedId, { silent: true });
    }
    scheduleAiAnalysisPolling(1200);
  } finally {
    syncWorkspaceActions();
  }
}

async function onStopAiAnalysis() {
  if (!state.projectId || !state.currentSorDocumentId) {
    alert("请先选择项目和 SOR 文档");
    return;
  }
  const jobStatus = String(state.aiAnalysisJob?.status || "").trim().toUpperCase();
  if (!["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(jobStatus)) {
    return;
  }
  if (jobStatus !== "CANCEL_REQUESTED") {
    const accepted = window.confirm("停止当前 AI 分析？已完成的条目会保留，未完成条目将停止继续消耗 token。");
    if (!accepted) {
      return;
    }
  }
  setAnalyzeStatus("已请求停止 AI 分析，正在等待当前调用安全中断。", "warning");
  try {
    const payload = await api(
      `/api/projects/${state.projectId}/sor-documents/${state.currentSorDocumentId}/run-ai-analysis/stop`,
      { method: "POST" }
    );
    applyAiAnalysisStatus(payload);
    scheduleAiAnalysisPolling(700);
  } finally {
    syncWorkspaceActions();
  }
}

function onAiFollowCurrentToggleChanged(event) {
  const nextFollow = Boolean(event?.target?.checked);
  state.aiFollowCurrentRequirement = nextFollow;
  syncAiFollowCurrentToggle();
  if (!nextFollow) {
    renderAiStage();
    syncWorkspaceActions();
    return;
  }
  const currentRequirementId = getCurrentAiRunningRequirementId();
  if (currentRequirementId) {
    void selectAiRequirement(currentRequirementId, { silent: true });
    return;
  }
  renderAiStage();
  syncWorkspaceActions();
}

function onAiLiveReasoningClicked(event) {
  const stopTrigger = event.target instanceof Element ? event.target.closest("[data-ai-stop]") : null;
  if (stopTrigger) {
    return onStopAiAnalysis();
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
  const hydratedSelection = await renderProjectOptions(projects);
  if (!hydratedSelection && state.projectId) {
    await onProjectChanged(state.currentSorDocumentId || null);
  }
}

async function renderProjectOptions(projects) {
  const current = state.projectId;
  const hasCurrent = Boolean(current) && projects.some((project) => project.id === current);
  el.projectSelect.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = projects.length ? "请选择项目" : "暂无项目，请先创建";
  el.projectSelect.appendChild(placeholder);

  projects.forEach((project) => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name} / ${project.customer_name}`;
    if (hasCurrent && project.id === current) {
      option.selected = true;
    }
    el.projectSelect.appendChild(option);
  });

  if (!hasCurrent && current) {
    state.projectId = null;
  }

  if (!state.projectId && projects.length) {
    state.projectId = projects[0].id;
    el.projectSelect.value = state.projectId;
    persistWorkspaceSelection();
    await onProjectChanged();
    return true;
  }

  el.projectSelect.value = state.projectId || "";
  return false;
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
  if (el.removeSorBtn) {
    el.removeSorBtn.disabled = !state.currentSorDocumentId;
  }
}

async function loadWorkspace(options = {}) {
  const skipAiStatus = Boolean(options?.skipAiStatus);
  stopReviewPreviewPolling();
  if (!state.projectId || !state.currentSorDocumentId) {
    state.workspace = null;
    state.knowledgeDocuments = [];
    resetAiAnalysisState();
    state.reviewPreview = null;
    state.requirements = [];
    state.reviewEntries = [];
    state.reviewSelectedKinds = [];
    state.reviewShowRecommendedSourceOnly = false;
    state.reviewActiveEntryId = "";
    state.reviewPdfDrawerOpen = false;
    initReviewPdfViewer();
    renderRequirements([]);
    renderKnowledgeDocuments();
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
  state.reviewSelectedKinds = [];
  state.reviewShowRecommendedSourceOnly = false;
  state.reviewActiveEntryId = "";
  state.reviewPdfDrawerOpen = false;
  initReviewPdfViewer();
  ensureAiRequirementSelection();
  renderRequirements(state.requirements);
  renderWorkspaceSummary();
  renderReviewDiagnostics();
  renderReviewKindFilter();
  renderReviewEntries();
  setReviewStatus("");
  renderAiStage();
  syncWorkspaceActions();
  if (!skipAiStatus) {
    await loadAiAnalysisStatus({ suppressErrors: true, refreshWorkspaceOnTerminal: false });
  }
  if (state.currentStage === "ai" && state.aiSelectedRequirementId && !state.aiRequirementDetailCache[state.aiSelectedRequirementId]) {
    void selectAiRequirement(state.aiSelectedRequirementId, { silent: true });
  }
  if (state.currentStage === "review") {
    await loadReviewPreview();
  }
}

async function loadKnowledgeDocuments() {
  if (!state.projectId) {
    state.knowledgeDocuments = [];
    state.knowledgeDeletingId = "";
    renderKnowledgeDocuments();
    return;
  }
  const docs = await api(`/api/projects/${state.projectId}/knowledge-documents`);
  state.knowledgeDocuments = Array.isArray(docs) ? docs : [];
  state.knowledgeDeletingId = "";
  renderKnowledgeDocuments();
}

async function onKnowledgeDocumentTableClicked(event) {
  const retryButton = event.target instanceof Element ? event.target.closest("[data-knowledge-sync-retry]") : null;
  if (retryButton) {
    const documentId = String(retryButton.getAttribute("data-knowledge-sync-retry") || "").trim();
    if (!documentId || !state.projectId || state.knowledgeSyncRetryingId || state.knowledgeDeletingId) {
      return;
    }
    state.knowledgeSyncRetryingId = documentId;
    renderKnowledgeDocuments();
    if (el.kbUploadResult) {
      el.kbUploadResult.textContent = "正在重试知识文档同步...";
    }
    try {
      await api(`/api/projects/${state.projectId}/knowledge-documents/${documentId}/resync`, {
        method: "POST",
      });
      await loadKnowledgeDocuments();
      if (state.projectId && state.currentSorDocumentId) {
        await loadWorkspace({ skipAiStatus: true });
      }
      if (el.kbUploadResult) {
        el.kbUploadResult.textContent = "知识文档同步已重试，请查看最新状态。";
      }
    } finally {
      state.knowledgeSyncRetryingId = "";
      renderKnowledgeDocuments();
    }
    return;
  }

  const removeButton = event.target instanceof Element ? event.target.closest("[data-knowledge-remove]") : null;
  if (!removeButton) {
    return;
  }
  const documentId = String(removeButton.getAttribute("data-knowledge-remove") || "").trim();
  if (!documentId || !state.projectId || state.knowledgeDeletingId || state.knowledgeSyncRetryingId) {
    return;
  }
  const targetDocument = state.knowledgeDocuments.find((item) => String(item.id || "") === documentId) || null;
  const fileName = String(targetDocument?.file_name || "该知识文档").trim() || "该知识文档";
  const accepted = window.confirm(`移除知识文档“${fileName}”？这会让当前 AI 结果标记为过期。`);
  if (!accepted) {
    return;
  }

  state.knowledgeDeletingId = documentId;
  renderKnowledgeDocuments();
  if (el.kbUploadResult) {
    el.kbUploadResult.textContent = "正在移除知识文档...";
  }
  try {
    const result = await api(`/api/projects/${state.projectId}/knowledge-documents/${documentId}`, {
      method: "DELETE",
    });
    await loadKnowledgeDocuments();
    if (state.projectId && state.currentSorDocumentId) {
      await loadWorkspace({ skipAiStatus: true });
    }
    if (el.kbUploadResult) {
      const removedCount = Number(result?.removed_count || 1);
      el.kbUploadResult.textContent = removedCount > 1
        ? `已移除: ${result.file_name}（含 ${removedCount} 条同名历史记录），知识库版本 ${result.knowledge_base_version}`
        : `已移除: ${result.file_name}，知识库版本 ${result.knowledge_base_version}`;
    }
  } finally {
    state.knowledgeDeletingId = "";
    renderKnowledgeDocuments();
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
  const selectedProfile = getSelectedAiProfile();
  const currentDoc = state.sorDocuments.find((doc) => doc.id === state.currentSorDocumentId) || null;
  const total = state.requirements.length;
  const finalCount = state.requirements.filter((item) => Boolean(item.final_decision)).length;
  const hybridCount = state.requirements.filter((item) => String(item.entry_kind || "").toUpperCase() === "HYBRID").length;
  const infoCount = state.requirements.filter((item) => String(item.analysis_label || "").toUpperCase() === "INFO").length;
  const cards = [
    ["当前文档", workspaceDoc.file_name || currentDoc?.file_name || "未选择"],
    ["工作流", formatWorkflowStage(workspaceDoc.workflow_stage)],
    ["条目集版本", String(workspaceDoc.requirement_set_version ?? 0)],
    ["AI 状态", formatAiStatus(workspaceDoc.ai_status, workspaceDoc.ai_stale)],
    ["预览状态", formatPreviewStatus(workspaceSummary.preview_status)],
    ["知识库版本", String(workspaceProject.knowledge_base_version ?? 0)],
    ["知识文档", String(workspaceSummary.knowledge_doc_count ?? 0)],
    ["当前 AI 路径", selectedProfile?.label || workspaceDoc.default_ai_profile_id || "未选择"],
    ["可运行路径", String(workspaceSummary.runnable_ai_profile_count ?? 0)],
    ["矩阵条目", String(total)],
    ["INFO 条目", String(infoCount)],
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
    const profileLabel = selectedProfile?.label || workspaceDoc.default_ai_profile_id || "未选择";
    el.workspaceStatus.textContent = `当前文档 ${workspaceDoc.file_name || currentDoc?.file_name || ""}，阶段 ${stage}，条目集版本 ${
      workspaceDoc.requirement_set_version ?? 0
    }，${locked}，AI ${aiStatus}，路径 ${profileLabel}，预览 ${previewStatus}。`;
  }
}

function renderKnowledgeDocuments() {
  if (!el.knowledgeDocsBody) {
    return;
  }
  const docs = Array.isArray(state.knowledgeDocuments) ? state.knowledgeDocuments : [];
  if (el.knowledgeDocsSummary) {
    el.knowledgeDocsSummary.textContent = docs.length ? `当前项目共 ${docs.length} 份知识文档。` : "当前项目还没有上传知识文档。";
  }
  if (!docs.length) {
    el.knowledgeDocsBody.innerHTML = `<tr><td colspan="3">暂无知识文档</td></tr>`;
    return;
  }
  el.knowledgeDocsBody.innerHTML = docs
    .map((doc) => {
      const syncs = Array.isArray(doc.retrieval_syncs) ? doc.retrieval_syncs : [];
      const syncStatus = syncs.length
        ? syncs
            .map((sync) => {
              const label = String(sync.profile_id || sync.provider || "").trim() || "default";
              const status = String(sync.status || "PENDING").trim();
              const syncedAt = String(sync.synced_at || "").trim();
              return `${label}: ${status}${syncedAt ? ` / ${formatTime(syncedAt)}` : ""}`;
            })
            .join("; ")
        : [String(doc.retrieval_sync_status || "PENDING").trim(), String(doc.retrieval_synced_at || "").trim() ? formatTime(doc.retrieval_synced_at) : ""]
            .filter(Boolean)
            .join(" / ");
      const canRetry = !syncs.length || syncs.some((sync) => String(sync.status || "PENDING").trim().toUpperCase() !== "READY");
      const retrying = state.knowledgeSyncRetryingId === String(doc.id || "");
      const deleting = state.knowledgeDeletingId === String(doc.id || "");
      const actionDisabled = retrying || deleting;
      const uploadedAt = formatTime(doc.created_at) || "-";
      return `
        <tr>
          <td class="knowledge-doc-name">
            <div class="knowledge-doc-title">${escapeHtml(String(doc.file_name || ""))}</div>
            <div class="knowledge-doc-meta">上传于 ${escapeHtml(formatTime(doc.created_at) || "-")}</div>
          </td>
          <td><div class="knowledge-doc-uploaded">${escapeHtml(uploadedAt)}</div></td>
          <td>
            <div class="knowledge-doc-sync">${escapeHtml(syncStatus || "-")}</div>
            <div class="knowledge-doc-actions">
              ${
                canRetry
                  ? `<button type="button" class="button-compact" data-knowledge-sync-retry="${escapeHtml(String(doc.id || ""))}"${
                      actionDisabled ? " disabled" : ""
                    }>${retrying ? "重试中..." : "重试同步"}</button>`
                  : ""
              }
              <button type="button" class="button-compact button-danger-subtle" data-knowledge-remove="${escapeHtml(
                String(doc.id || "")
              )}"${actionDisabled ? " disabled" : ""}>${deleting ? "移除中..." : "移除文档"}</button>
            </div>
          </td>
        </tr>`;
    })
    .join("");
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
      ["顺序模型", summary.section_order_model || "-"],
      ["完整条目", summary.complete_entries_count ?? 0],
      ["原始条目", summary.complete_entries_raw_count ?? 0],
      ["章节数", summary.sections_count ?? 0],
      ["章节树节点", summary.section_tree_count ?? summary.section_outline_count ?? 0],
      ["一致性问题", summary.section_consistency_issues_total ?? 0],
      ["零直挂父章", summary.zero_direct_requirement_parent_count ?? 0],
      ["Synthetic 章节", summary.synthetic_section_count ?? 0],
      ["Orphan 章节", summary.orphan_section_count ?? 0],
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
    const rows = buildReviewSectionOverviewRows(preview.section_tree || preview.sections || []);
    const levelCount = Math.max(
      1,
      ...rows.map((row) => (Array.isArray(row.level_path) ? row.level_path.length : 0))
    );
    renderSectionOverviewHead(el.reviewSectionOverviewBody, levelCount, ["页范围"]);
    if (!rows.length) {
      el.reviewSectionOverviewBody.innerHTML = `<tr><td colspan="${levelCount + 2}">暂无章节数据</td></tr>`;
    } else {
      el.reviewSectionOverviewBody.innerHTML = rows
        .map(
          (row) => {
            const span = row.source_page_start ? `${row.source_page_start}-${row.source_page_end || row.source_page_start}` : "-";
            const scopeTitle = row.appendix_title ? ` title="${escapeHtml(row.appendix_title)}"` : "";
            return `<tr>
            <td><span class="section-overview-scope-pill"${scopeTitle}>${escapeHtml(formatScope(row.appendix_scope, row.appendix_title))}</span></td>
            ${renderSectionOverviewPathCells(row, levelCount)}
            <td class="section-overview-metric-cell"><span class="section-overview-page-pill">${escapeHtml(span)}</span></td>
          </tr>`;
          }
        )
        .join("");
    }
  }
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

function buildReviewSectionOverviewRows(rows) {
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

  return sourceRows.map((row) => {
    const path = resolvePath(row).filter((pathRow) => !pathRow?.is_appendix_scope_root);
    return {
      appendix_scope: row.appendix_scope || "",
      appendix_title: row.appendix_title || "",
      section_code: row.section_code || "",
      section_title: row.section_title || "",
      numbering_level: Number(row.numbering_level ?? row.section_level ?? 0) || 0,
      structural_level: Number(row.structural_level ?? resolveDepth(row)) || 0,
      tree_depth: resolveDepth(row),
      node_label: buildSectionOverviewNodeLabel(row),
      parent_label: buildSectionOverviewParentLabel(row),
      is_appendix_scope_root: Boolean(row.is_appendix_scope_root),
      source_page_start: row.source_page_start || "",
      source_page_end: row.source_page_end || "",
      requirement_count: row.requirement_count || 0,
      title_meta: buildSectionOverviewTitleMeta({ ...row, node_label: buildSectionOverviewNodeLabel(row) }),
      level_path: path.map((pathRow) => ({
        label: buildSectionOverviewNodeLabel(pathRow),
        is_root: Boolean(pathRow.is_appendix_scope_root),
      })),
    };
  });
}

function renderReviewKindFilter() {
  if (!el.reviewKindFilter) {
    return;
  }
  const kinds = new Set((state.reviewEntries || []).map((item) => String(item.entry_kind || "REQUIREMENT").toUpperCase()));
  const selectedKinds = new Set((Array.isArray(state.reviewSelectedKinds) ? state.reviewSelectedKinds : []).map((kind) => String(kind || "").toUpperCase()));
  const chips = el.reviewKindFilter.querySelectorAll(".kind-chip");
  chips.forEach((chip) => {
    const kind = chip.dataset.kind;
    const available = kinds.has(kind);
    chip.style.display = available ? "" : "none";
    const shouldBeActive = available && (selectedKinds.size === 0 || selectedKinds.has(kind));
    chip.classList.toggle("is-active", shouldBeActive);
  });
  state.reviewSelectedKinds = getSelectedReviewKinds();
}

function getSelectedReviewKinds() {
  if (!el.reviewKindFilter) {
    return [];
  }
  const chips = el.reviewKindFilter.querySelectorAll(".kind-chip.is-active");
  return Array.from(chips).map((chip) => chip.dataset.kind);
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
  const selectedKinds = state.reviewSelectedKinds || [];
  return (rows || []).filter((row) => {
    const entryKind = String(row.entry_kind || "REQUIREMENT").toUpperCase();
    if (selectedKinds.length && !selectedKinds.includes(entryKind)) {
      return false;
    }
    const sectionKey = [
      row.owner_section_label,
      row.owner_section_code,
      row.owner_section_title,
      row.section_label,
      row.section_code,
      row.section_title,
      row.parent_section_label,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
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
    section_code: row.section_code || "",
    section_title: row.section_title || "",
    section_label: row.section_label || "",
    section_key: row.section_key || "",
    section_instance_key: row.section_instance_key || "",
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
    owner_section_code: row.owner_section_code || "",
    owner_section_title: row.owner_section_title || "",
    owner_section_label: row.owner_section_label || "",
    structural_parent_section_code: row.structural_parent_section_code || "",
    structural_parent_section_key: row.structural_parent_section_key || "",
    structural_parent_section_title: row.structural_parent_section_title || "",
    structural_parent_section_label: row.structural_parent_section_label || "",
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
  ensureAiRequirementSelection();
  renderRequirements(state.requirements);
  renderWorkspaceSummary();
  renderReviewDiagnostics();
  renderReviewKindFilter();
  renderReviewEntries();
  renderAiStage();
  syncWorkspaceActions();
  if (state.currentStage === "ai" && state.aiSelectedRequirementId && !state.aiRequirementDetailCache[state.aiSelectedRequirementId]) {
    void selectAiRequirement(state.aiSelectedRequirementId, { silent: true });
  }
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
  const detailButton = target.closest("[data-ai-detail]");
  if (detailButton instanceof HTMLButtonElement) {
    openAiRequirementDetail(String(detailButton.getAttribute("data-ai-detail") || ""));
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
  return `/assets/pdf-viewer.html?v=20260403-reqx1&pdf=${encodedPdf}&page=${Number(pageNo || 1)}`;
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
  renderAiProfileSelector();
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
    const summary = state.workspace?.summary || {};
    const job = state.aiAnalysisJob || {};
    const jobStatus = String(job.status || "").trim().toUpperCase();
    const selectedProfile = getSelectedAiProfile();
    const activeProfileLabel = String(job.profile_id || "").trim()
      ? `${job.profile_id}${job.provider ? ` / ${job.provider}` : ""}`
      : selectedProfile?.label || document.default_ai_profile_id || "未选择";
    const statusItems = [
      ["阶段", formatWorkflowStage(document.workflow_stage)],
      ["AI 状态", formatAiStatus(document.ai_status, document.ai_stale)],
      ["任务", formatAiJobStatus(jobStatus || "IDLE")],
      ["步骤", formatAiJobStage(job.current_stage || document.workflow_stage || "queued")],
      ["条目集", String(document.requirement_set_version ?? 0)],
      ["运行路径", activeProfileLabel],
      ["AI 模式", String(job.assessment_mode || selectedProfile?.assessment_mode || document.configured_assessment_mode || "heuristic")],
      ["Retrieval", String(job.retrieval_backend || selectedProfile?.retrieval_backend || summary.configured_retrieval_backend || "local")],
      ["认证", String(job.provider === "aliyun" ? "api_key" : selectedProfile?.auth_mode || summary.configured_azure_auth_mode || "auto")],
    ];
    el.aiStageStatus.innerHTML = `
      <div class="ai-stage-status-grid">
        ${statusItems
          .map(
            ([label, value]) => `
              <div class="ai-stage-status-item">
                <div class="ai-stage-status-label">${escapeHtml(label)}</div>
                <div class="ai-stage-status-value">${escapeHtml(String(value || "-"))}</div>
              </div>
            `
          )
          .join("")}
      </div>
    `;
  }
  renderAiProgressPanel();
  renderAiRequirementInspector();
}

function renderAiProgressPanel() {
  const job = state.aiAnalysisJob || null;
  const events = Array.isArray(state.aiAnalysisEvents) ? state.aiAnalysisEvents : [];
  const jobStatus = String(job?.status || "").trim().toUpperCase();
  const isTerminalJob = ["COMPLETED", "FAILED", "CANCELLED"].includes(jobStatus);
  if (el.aiJobSummary) {
    el.aiJobSummary.textContent = job
      ? `${formatAiJobStatus(job.status)} · ${formatAiJobStage(job.current_stage)} · ${formatTime(job.updated_at || job.created_at) || "刚刚"}`
      : "尚未运行 AI 分析";
  }
  if (el.aiProgressCurrent) {
    el.aiProgressCurrent.className = `ai-progress-current${isTerminalJob ? " is-compact" : ""}`;
    if (!job) {
      el.aiProgressCurrent.innerHTML = `<div class="ai-progress-empty">锁定条目集后运行 AI，页面会在这里持续显示当前阶段、处理进度和步骤日志。</div>`;
    } else {
      const total = Number(job.total_requirements || 0);
      const completed = Number(job.completed_requirements || 0);
      const progress = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
      const renderProgressMetric = (label, value) =>
        `<div class="ai-progress-metric"><div class="ai-progress-metric-label">${escapeHtml(label)}</div><div class="ai-progress-metric-value">${escapeHtml(String(value))}</div></div>`;
      const renderInlineMetric = (label, value) =>
        `<div class="ai-progress-inline-metric"><span class="ai-progress-inline-metric-label">${escapeHtml(label)}</span><strong class="ai-progress-inline-metric-value">${escapeHtml(String(value))}</strong></div>`;
      const metricsMarkup = isTerminalJob
        ? `
          <div class="ai-progress-inline-metrics">
            ${renderInlineMetric("总条目", total)}
            ${renderInlineMetric("已完成", completed)}
            ${renderInlineMetric("已判定", job.analyzed_requirements || 0)}
            ${renderInlineMetric("INFO / 待分析", `${job.info_requirements || 0} / ${job.screened_requirements || 0}`)}
          </div>
        `
        : `
          <div class="ai-progress-metrics">
            ${renderProgressMetric("总条目", total)}
            ${renderProgressMetric("已完成", completed)}
            ${renderProgressMetric("已判定", job.analyzed_requirements || 0)}
            ${renderProgressMetric("INFO / 待分析", `${job.info_requirements || 0} / ${job.screened_requirements || 0}`)}
          </div>
        `;
      el.aiProgressCurrent.innerHTML = `
        <div class="ai-progress-head">
          <div>
            <div class="ai-progress-label">${escapeHtml(formatAiJobStatus(job.status))}</div>
            <p class="ai-progress-message">${escapeHtml(String(job.last_message || "AI 分析已排队。"))}</p>
          </div>
          ${decisionChip(
            job.status === "FAILED"
              ? "REJECT"
              : job.status === "COMPLETED"
              ? "ACCEPT"
              : job.status === "CANCELLED"
              ? "STOPPED"
              : job.status === "CANCEL_REQUESTED"
              ? "STOPPING"
              : "NEED_CLARIFICATION"
          )}
        </div>
        <div class="ai-progress-bar"><span style="width: ${progress}%;"></span></div>
        ${metricsMarkup}
      `;
    }
  }
  renderAiLiveReasoning();
  if (el.aiProgressTimeline) {
    const timelineEvents = getAiTimelineEvents(events);
    if (!timelineEvents.length) {
      el.aiProgressTimeline.innerHTML = `<div class="ai-progress-empty">暂无步骤日志。</div>`;
      return;
    }
    el.aiProgressTimeline.innerHTML = timelineEvents
      .map((event) => {
        const meta = [
          formatAiJobStage(event.stage),
          event.requirement_code ? `需求 ${event.requirement_code}` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        const detailPreview = getAiEventDetailPreview(event);
        return `
          <article class="ai-progress-event ${getAiEventToneClass(event)}">
            <div class="ai-progress-event-head">
              <div class="ai-progress-event-type">${escapeHtml(formatAiEventType(event.event_type))}</div>
              <div class="ai-progress-event-time">${escapeHtml(formatTime(event.created_at) || "刚刚")}</div>
            </div>
            <p class="ai-progress-event-message">${escapeHtml(String(event.message || ""))}</p>
            ${detailPreview ? `<pre class="ai-progress-event-stream">${escapeHtml(detailPreview)}</pre>` : ""}
            ${meta ? `<div class="ai-progress-event-meta">${escapeHtml(meta)}</div>` : ""}
          </article>
        `;
      })
      .join("");
  }
}

function getCurrentAiReasoningSnapshot() {
  const job = state.aiAnalysisJob || {};
  const currentRequirementId = String(job.current_requirement_id || "").trim();
  if (!currentRequirementId) {
    return null;
  }
  let latestStageEvent = null;
  let latestStreamEvent = null;
  for (const event of Array.isArray(state.aiAnalysisEvents) ? state.aiAnalysisEvents : []) {
    if (String(event?.requirement_id || "").trim() !== currentRequirementId) {
      continue;
    }
    const eventType = String(event?.event_type || "").trim().toLowerCase();
    if (eventType === "requirement_stage_update") {
      latestStageEvent = event;
    }
    if (AI_STREAM_EVENT_TYPES.has(eventType)) {
      latestStreamEvent = event;
    }
  }
  if (!latestStageEvent && !latestStreamEvent) {
    return null;
  }
  const latestEvent = latestStreamEvent || latestStageEvent;
  const payload = latestEvent?.payload || {};
  const eventType = String(latestEvent?.event_type || "").trim().toLowerCase();
  const rawPreview = String(payload.content_preview || "").trim();
  const trimmedPreview = trimAiTextPreview(rawPreview, AI_LIVE_PREVIEW_LIMIT);
  return {
    requirementCode: String(latestEvent?.requirement_code || job.current_requirement_code || "").trim(),
    stage: String(payload.stage || latestEvent?.stage || job.current_stage || "").trim().toLowerCase(),
    phase: inferAiLivePhase(eventType, payload),
    contentPreview: trimmedPreview,
    charactersEmitted: Number(payload.characters_emitted || 0),
    truncated: Boolean(payload.truncated) || trimmedPreview.length < rawPreview.length,
    mode: String(payload.mode || "").trim(),
    modelName: String(payload.model_name || "").trim(),
    provider: String(payload.provider || latestEvent?.provider || job.provider || "").trim(),
    updatedAt: String(latestEvent?.created_at || "").trim(),
    message: String(latestEvent?.message || "").trim(),
    sourceEventType: eventType,
    hasModelReasoning:
      eventType === "requirement_reasoning" || eventType === "requirement_screening_reasoning",
  };
}

function renderAiLiveReasoning() {
  if (!el.aiLiveReasoning) {
    return;
  }
  const job = state.aiAnalysisJob || {};
  const status = String(job.status || "").trim().toUpperCase();
  const jobStatus = status;
  if (!["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(status)) {
    el.aiLiveReasoning.innerHTML = "";
    el.aiLiveReasoning.classList.remove("is-visible");
    return;
  }
  const snapshot = getCurrentAiReasoningSnapshot();
  if (!snapshot) {
    el.aiLiveReasoning.innerHTML = `
      <div class="ai-live-reasoning-empty">
        AI 运行后，这里会按“筛选 → 检索 → 分析”展示当前条目的实时轨迹。Aliyun 链路会展示 reasoning 片段，Azure 链路会展示结构化输出草稿。
      </div>
    `;
    el.aiLiveReasoning.classList.add("is-visible");
    return;
  }
  const badges = [
    `<span class="ai-live-badge is-primary">实时链路</span>`,
    `<span class="ai-live-badge">${escapeHtml(formatAiLiveStage(snapshot.stage || "screening"))}</span>`,
    `<span class="ai-live-badge">${escapeHtml(formatAiLivePhase(snapshot))}</span>`,
  ];
  if (snapshot.provider) {
    badges.push(`<span class="ai-live-badge is-muted">${escapeHtml(formatAiProvider(snapshot.provider))}</span>`);
  }
  if (snapshot.mode) {
    badges.push(`<span class="ai-live-badge is-muted">${escapeHtml(snapshot.mode)}</span>`);
  }
  const stats = [
    ["当前条目", snapshot.requirementCode || "-"],
    ["当前模型", snapshot.modelName || "运行中"],
    ["最近更新", formatTime(snapshot.updatedAt) || "刚刚"],
    ["输出进度", snapshot.charactersEmitted > 0 ? `${snapshot.charactersEmitted} 字` : "等待首个片段"],
    ["停止状态", jobStatus === "CANCEL_REQUESTED" ? "停止请求已发出" : "运行中"],
  ];
  const bodyText = snapshot.contentPreview || snapshot.message || "等待首个片段...";
  const stageTrack = renderAiLiveStageTrack(snapshot, jobStatus);
  const stopButtonLabel = jobStatus === "CANCEL_REQUESTED" ? "停止中..." : "停止本次运行";
  el.aiLiveReasoning.innerHTML = `
    <div class="ai-live-reasoning-top">
      <div class="ai-live-badges">${badges.join("")}</div>
      <div class="ai-live-reasoning-head">
        <div>
          <div class="ai-live-reasoning-label">${escapeHtml(formatAiLiveHeadline(snapshot))}</div>
          <div class="ai-live-reasoning-meta">${escapeHtml(snapshot.message || "模型正在处理当前条目。")}</div>
        </div>
        <div class="ai-live-reasoning-head-actions">
          <button
            type="button"
            class="button-compact ai-live-stop-btn"
            data-ai-stop="true"
            ${jobStatus === "CANCEL_REQUESTED" ? "disabled" : ""}
          >${escapeHtml(stopButtonLabel)}</button>
          <span class="ai-live-reasoning-pulse" aria-hidden="true"></span>
        </div>
      </div>
    </div>
    ${stageTrack}
    <div class="ai-live-reasoning-grid">
      <div class="ai-live-reasoning-main">
        <pre class="ai-live-reasoning-body">${escapeHtml(bodyText)}</pre>
        <div class="ai-live-reasoning-advisory">${escapeHtml(getAiLiveAdvisory(snapshot))}</div>
      </div>
      <div class="ai-live-reasoning-stats">
        ${stats
          .map(
            ([label, value]) => `
              <div class="ai-live-stat">
                <div class="ai-live-stat-label">${escapeHtml(label)}</div>
                <div class="ai-live-stat-value">${escapeHtml(String(value || "-"))}</div>
              </div>
            `
          )
          .join("")}
      </div>
    </div>
  `;
  el.aiLiveReasoning.classList.add("is-visible");
}

function getAiEventDetailPreview(event) {
  const eventType = String(event?.event_type || "").trim().toLowerCase();
  if (!AI_STREAM_EVENT_TYPES.has(eventType)) {
    return "";
  }
  return trimAiTextPreview(String(event?.payload?.content_preview || "").trim(), AI_TIMELINE_PREVIEW_LIMIT);
}

function getAiTimelineEvents(events) {
  const windowed = Array.isArray(events) ? events.slice(-80) : [];
  const filtered = [];
  for (let index = 0; index < windowed.length; index += 1) {
    const event = windowed[index];
    const eventType = String(event?.event_type || "").trim().toLowerCase();
    if (eventType === "requirement_answer_started") {
      const hasLaterOutput = windowed.slice(index + 1).some((candidate) => {
        return (
          String(candidate?.requirement_id || "").trim() === String(event?.requirement_id || "").trim() &&
          String(candidate?.event_type || "").trim().toLowerCase() === "requirement_output"
        );
      });
      if (hasLaterOutput) {
        continue;
      }
    }
    filtered.push(event);
  }
  return filtered.slice(-AI_TIMELINE_RENDER_LIMIT);
}

function inferAiLivePhase(eventType, payload) {
  const explicitPhase = String(payload?.phase || "").trim().toLowerCase();
  if (explicitPhase) {
    return explicitPhase;
  }
  if (["requirement_reasoning", "requirement_screening_reasoning"].includes(eventType)) {
    return "reasoning";
  }
  if (AI_STREAM_EVENT_TYPES.has(eventType)) {
    return "output";
  }
  return "pending";
}

function trimAiTextPreview(text, limit) {
  const normalized = String(text || "").trim();
  if (!normalized || normalized.length <= limit) {
    return normalized;
  }
  return `…${normalized.slice(-limit)}`;
}

function formatAiLiveStage(stage) {
  const normalized = String(stage || "").trim().toLowerCase();
  const mapping = {
    screening: "筛选",
    retrieval: "检索",
    assessment: "分析",
  };
  return mapping[normalized] || "处理中";
}

function formatAiLivePhase(snapshot) {
  const stage = String(snapshot?.stage || "").trim().toLowerCase();
  const phase = String(snapshot?.phase || "").trim().toLowerCase();
  if (stage === "screening") {
    if (phase === "reasoning") {
      return "筛选推理";
    }
    if (phase === "output") {
      return "筛选输出";
    }
    return "筛选准备";
  }
  if (stage === "retrieval") {
    return "证据检索";
  }
  if (phase === "reasoning") {
    return "模型推理";
  }
  if (phase === "output") {
    return "结构化输出";
  }
  return "处理中";
}

function formatAiLiveHeadline(snapshot) {
  const stage = String(snapshot?.stage || "").trim().toLowerCase();
  const phase = String(snapshot?.phase || "").trim().toLowerCase();
  if (stage === "screening") {
    return phase === "reasoning" ? "正在筛选推理" : phase === "output" ? "正在生成筛选结果" : "正在准备筛选";
  }
  if (stage === "retrieval") {
    return "正在检索候选证据";
  }
  return phase === "reasoning" ? "正在推理" : phase === "output" ? "正在生成结构化结论" : "正在分析";
}

function formatAiProvider(provider) {
  const normalized = String(provider || "").trim().toLowerCase();
  if (normalized === "aliyun") {
    return "Aliyun";
  }
  if (normalized === "azure") {
    return "Azure";
  }
  return provider || "Provider";
}

function renderAiLiveStageTrack(snapshot, jobStatus) {
  const currentStage = String(snapshot?.stage || "").trim().toLowerCase();
  const currentIndex = AI_STAGE_ORDER.indexOf(currentStage);
  const normalizedStatus = String(jobStatus || "").trim().toUpperCase();
  const completedJob = normalizedStatus === "COMPLETED";
  const stoppingJob = normalizedStatus === "CANCEL_REQUESTED";
  const hints = {
    screening: "判定该条目是否进入需求分析",
    retrieval: "召回候选证据并过滤噪声块",
    assessment: "生成结构化判断并校验输出",
  };
  return `
    <div class="ai-live-stage-track">
      ${AI_STAGE_ORDER.map((stage, index) => {
        const classes = ["ai-live-stage-step"];
        if (completedJob || (currentIndex >= 0 && index < currentIndex)) {
          classes.push("is-complete");
        }
        if (stage === currentStage) {
          classes.push("is-active");
        }
        if ((normalizedStatus === "FAILED" || normalizedStatus === "CANCELLED" || stoppingJob) && stage === currentStage) {
          classes.push("is-failed");
        }
        return `
          <div class="${classes.join(" ")}">
            <div class="ai-live-stage-name">${escapeHtml(formatAiLiveStage(stage))}</div>
            <div class="ai-live-stage-hint">${escapeHtml(hints[stage] || "")}</div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function getAiLiveAdvisory(snapshot) {
  const jobStatus = String(state.aiAnalysisJob?.status || "").trim().toUpperCase();
  if (jobStatus === "CANCEL_REQUESTED") {
    return "已请求停止本次 AI 运行；当前调用会在安全中断点尽快结束，避免继续浪费 token。";
  }
  if (snapshot.hasModelReasoning) {
    return "展示模型 reasoning 片段；随着模型继续推理，内容可能被补充或修正。";
  }
  if (snapshot.stage === "screening") {
    return "当前展示筛选阶段的结构化输出草稿，最终是否继续分析以校验后的筛选结果为准。";
  }
  if (String(snapshot.provider || "").trim().toLowerCase() === "azure") {
    return "Azure 当前链路展示的是结构化输出草稿，不一定包含隐藏 reasoning。";
  }
  return "当前展示结构化输出草稿，最终落库结果以校验后的 JSON 为准。";
}

function renderAiRequirementInspector() {
  const rows = getAiInspectableRequirements();
  const selectedId = ensureAiRequirementSelection();
  syncAiFollowCurrentToggle();
  const detail = selectedId ? state.aiRequirementDetailCache[selectedId] : null;
  const loading = Boolean(selectedId) && state.aiRequirementDetailLoadingId === selectedId;
  const currentRequirementId = getCurrentAiRunningRequirementId();
  if (el.aiDetailSummary) {
    if (!selectedId) {
      el.aiDetailSummary.textContent = rows.length
        ? "从左侧选择一个条目查看证据链。"
        : "AI 运行后会在这里展示条目级证据链和最终结论。";
    } else if (loading) {
      el.aiDetailSummary.textContent = "正在加载条目级证据链...";
    } else {
      const reqCode = detail?.requirement?.display_req_code || detail?.requirement?.req_code || selectedId;
      const followStateText = currentRequirementId
        ? state.aiFollowCurrentRequirement
          ? "已跟随当前运行条目"
          : "已暂停跟随当前运行条目"
        : "当前无运行中条目";
      el.aiDetailSummary.textContent = `当前查看：${reqCode} · ${followStateText}`;
    }
  }
  if (el.aiRequirementList) {
    if (!rows.length) {
      el.aiRequirementList.innerHTML = `<div class="ai-requirement-empty">暂无可展示的 AI 条目。运行分析后会在这里列出每条需求的最终结论。</div>`;
    } else {
      el.aiRequirementList.innerHTML = rows
        .map((row) => {
          const requirementId = String(row.id || "");
          const outcome = getAiOutcome(row);
          const activeClass = requirementId === selectedId ? "is-active" : "";
          return `
            <button class="ai-requirement-item ${activeClass}" type="button" data-ai-requirement="${escapeHtml(requirementId)}">
              <div class="ai-requirement-item-head">
                <div class="ai-requirement-code">${escapeHtml(String(row.display_req_code || row.req_code || requirementId))}</div>
                ${decisionChip(outcome)}
              </div>
              <div class="ai-requirement-snippet">${escapeHtml(String(row.display_text || row.requirement_text || row.raw_text || "").trim())}</div>
              <div class="ai-requirement-meta">${escapeHtml(renderParentSectionPlainText(row))}</div>
            </button>
          `;
        })
        .join("");
      syncAiRequirementViewports(selectedId);
    }
  }
  if (el.aiRequirementDetail) {
    if (!selectedId) {
      el.aiRequirementDetail.innerHTML = `<div class="ai-requirement-empty">选择一个条目后，可查看当前阶段、证据链、模型运行记录和最终结论。</div>`;
      return;
    }
    if (loading && !detail) {
      el.aiRequirementDetail.innerHTML = `<div class="ai-requirement-empty">正在加载条目级证据链...</div>`;
      return;
    }
    const requirement = detail?.requirement || getAiInspectableRequirements().find((row) => String(row.id || "") === selectedId) || {};
    const assessment = detail?.assessment || {};
    const normalization = detail?.normalization || {};
    const evidences = Array.isArray(detail?.evidences) ? detail.evidences : [];
    const runs = Array.isArray(detail?.assessment_runs) ? detail.assessment_runs : [];
    const outcome = getAiOutcome(requirement, assessment);
    const currentStageText = String(assessment.analysis_basis || requirement.analysis_basis || "").trim() || "当前暂无阶段说明。";
    const finalConclusion =
      String(assessment.ai_reason || requirement.ai_reason || "").trim() ||
      (outcome === "SCREENING_FAILED" || outcome === "ANALYSIS_FAILED"
        ? currentStageText
        : "暂无最终结论。");
    const clarificationQuestion = String(assessment.clarification_question || "").trim();
    const riskLevel = String(assessment.risk_level || "").trim().toUpperCase();
    el.aiRequirementDetail.innerHTML = `
      <div class="ai-detail-header">
        <div>
          <h3 class="ai-detail-title">${escapeHtml(String(requirement.display_req_code || requirement.req_code || selectedId))}</h3>
          <div class="ai-detail-meta">${escapeHtml(renderParentSectionPlainText(requirement))}</div>
        </div>
        <div>
          ${decisionChip(outcome)}
          ${riskLevel ? `<div class="knowledge-doc-meta">风险 ${escapeHtml(riskLevel)}</div>` : ""}
        </div>
      </div>
      <div class="ai-detail-block">
        <h4>原始需求</h4>
        <p>${escapeHtml(String(requirement.display_text || requirement.requirement_text || requirement.raw_text || "").trim() || "-")}</p>
      </div>
      <div class="ai-detail-block">
        <h4>当前阶段</h4>
        <p>${escapeHtml(currentStageText)}</p>
      </div>
      <div class="ai-detail-block">
        <h4>最终结论</h4>
        <p>${escapeHtml(finalConclusion)}</p>
      </div>
      ${renderAiNormalizationPanel({
        summary: normalization.summary || assessment.normalized_requirement || "",
        facets: normalization.facets || [],
      })}
      ${clarificationQuestion ? `<div class="ai-detail-block"><h4>待确认问题</h4><p>${escapeHtml(clarificationQuestion)}</p></div>` : ""}
      <div class="ai-detail-block">
        <h4>证据链</h4>
        ${renderAiEvidenceList(evidences)}
      </div>
      <div class="ai-detail-block">
        <h4>模型运行记录</h4>
        ${renderAiRunList(runs)}
      </div>
    `;
  }
}

function syncWorkspaceActions() {
  const workspace = state.workspace || {};
  const actions = workspace.actions || {};
  const selectedProfile = getSelectedAiProfile();
  const defaultKnowledgeProfile = getConfiguredDefaultProfile();
  const hasDoc = Boolean(state.projectId && state.currentSorDocumentId);
  const workflowStage = String(workspace.document?.workflow_stage || "").toUpperCase();
  const aiStatus = String(workspace.document?.ai_status || "NOT_RUN").toUpperCase();
  const aiJobStatus = String(state.aiAnalysisJob?.status || "").toUpperCase();
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
  if (el.kbUploadBtn) {
    const canUploadKnowledge = Boolean(state.projectId) && Boolean(defaultKnowledgeProfile?.retrieval_ready);
    el.kbUploadBtn.disabled = !canUploadKnowledge;
    el.kbUploadBtn.title = canUploadKnowledge
      ? ""
      : `请先完成默认 AI 路径配置。${formatAiProfileHint(defaultKnowledgeProfile)}`;
  }
  if (el.kbFile) {
    el.kbFile.disabled = !Boolean(state.projectId) || !Boolean(defaultKnowledgeProfile?.retrieval_ready);
  }
  if (el.runAiBtn) {
    const aiRunning =
      aiStatus === "RUNNING" ||
      aiJobStatus === "PENDING" ||
      aiJobStatus === "RUNNING" ||
      aiJobStatus === "CANCEL_REQUESTED";
    el.runAiBtn.disabled = !hasDoc || !Boolean(actions.can_run_ai_analysis) || !Boolean(selectedProfile?.ready) || aiRunning;
    if (aiRunning) {
      el.runAiBtn.textContent = aiJobStatus === "CANCEL_REQUESTED" ? "AI 停止中..." : "AI 运行中...";
    } else if (aiStale || aiStatus === "READY") {
      el.runAiBtn.textContent = "重新运行 AI";
    } else {
      el.runAiBtn.textContent = "运行 AI 分析";
    }
    el.runAiBtn.title = selectedProfile?.ready ? "" : formatAiProfileHint(selectedProfile);
  }
  if (el.stopAiBtn) {
    const canStop =
      hasDoc &&
      Boolean(state.aiAnalysisJob?.has_live_worker) &&
      ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(aiJobStatus);
    el.stopAiBtn.disabled = !canStop || aiJobStatus === "CANCEL_REQUESTED";
    el.stopAiBtn.textContent = aiJobStatus === "CANCEL_REQUESTED" ? "停止中..." : "停止 AI";
    el.stopAiBtn.title = canStop ? "停止当前 live AI 运行" : "当前没有可停止的 AI 运行";
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

function onReviewKindFilterChanged(event) {
  const chip = event.target?.closest?.(".kind-chip");
  if (chip) {
    chip.classList.toggle("is-active");
  }
  state.reviewSelectedKinds = getSelectedReviewKinds();
  renderReviewEntries();
}

function renderRequirements(items) {
  renderMatrixStatus();
  el.tableBody.innerHTML = "";
  if (!items.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="5">暂无需求数据，请上传并分析SOR文档。</td>`;
    el.tableBody.appendChild(row);
    return;
  }

  [...items]
    .sort((left, right) => Number(left.display_sort_index || 0) - Number(right.display_sort_index || 0))
    .forEach((item) => {
    const row = document.createElement("tr");
    const analysisLabel = getAnalysisLabel(item);
    const aiDecision = analysisLabel === "INFO" ? "" : String(item.ai_decision || "").trim().toUpperCase();
    const finalDecision = String(item.final_decision || aiDecision || "NEED_CLARIFICATION").trim().toUpperCase();
    const infoOnly = analysisLabel === "INFO";
    const entryKind = String(item.entry_kind || "REQUIREMENT").toUpperCase();
    const textMeta = [];
    if (entryKind === "HYBRID") {
      textMeta.push(`<span class="mini-chip mini-chip-hybrid">HYBRID</span>`);
    }
    if (entryKind === "HEADING") {
      textMeta.push(`<span class="mini-chip mini-chip-delta">HEADING</span>`);
    }
    row.innerHTML = `
      <td class="matrix-locator">${renderMatrixLocatorCell(item)}</td>
      <td class="req-text">${
        textMeta.length ? `<div class="req-meta">${textMeta.join("")}</div>` : ""
      }${renderRequirementContent(item)}</td>
      <td class="ai-result">${renderAiAssessmentCell(item)}</td>
      <td class="matrix-manual">${renderManualDecisionCell(item.id, finalDecision, infoOnly)}</td>
      <td><textarea class="matrix-comment-input" data-reason="${item.id}" placeholder="填写 Schaeffler Comments" ${infoOnly ? "disabled" : ""}>${escapeHtml(
        item.engineer_reason || ""
      )}</textarea></td>
    `;
    el.tableBody.appendChild(row);
  });

  el.tableBody.querySelectorAll("[data-save]").forEach((button) => {
    button.addEventListener("click", wrap(onSaveDecision));
  });
  el.tableBody.querySelectorAll("[data-reclassify]").forEach((button) => {
    button.addEventListener("click", wrap(onReclassifyRequirement));
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

async function onReclassifyRequirement(event) {
  const requirementId = event.currentTarget.getAttribute("data-reclassify");
  await api(`/api/requirements/${requirementId}/analysis-label`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      analysis_label: "REQUIREMENT",
      rerun_assessment: true,
      reason: "人工覆盖：该条目应作为真实需求继续分析。",
    }),
  });
  setAnalyzeStatus(`已将 ${requirementId} 改为 REQUIREMENT，并重新进入 AI 分析。`, "success");
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

function renderInfoDecisionPlaceholder() {
  return `<div class="matrix-muted">INFO 条目不参与人工判定</div>`;
}

function renderInfoActionCell(requirementId) {
  return `
    <div class="row-action-stack">
      <button data-reclassify="${escapeHtml(String(requirementId || ""))}" type="button">改为需求</button>
      <span class="matrix-muted">改后会重新进入 AI 分析</span>
    </div>
  `;
}

function renderMatrixLocatorCell(item) {
  const reqCode = String(item.display_req_code || item.req_code || "-").trim() || "-";
  const parentSection = renderParentSectionPlainText(item);
  return `
    <div class="matrix-locator-cell">
      <div class="matrix-locator-code">${escapeHtml(reqCode)}</div>
      <div class="matrix-locator-section">${escapeHtml(parentSection)}</div>
    </div>
  `;
}

function renderManualDecisionCell(requirementId, finalDecision, infoOnly) {
  if (infoOnly) {
    return `<div class="matrix-manual-cell matrix-manual-cell-info">${renderInfoDecisionPlaceholder()}${renderInfoActionCell(
      requirementId
    )}</div>`;
  }
  return `
    <div class="matrix-manual-cell">
      ${decisionSelect(requirementId, finalDecision)}
      <button data-save="${escapeHtml(String(requirementId || ""))}" type="button" class="button-compact matrix-save-btn">保存</button>
    </div>
  `;
}

function decisionChip(decision) {
  const map = {
    ACCEPT: ["chip-accept", "ACCEPT"],
    REJECT: ["chip-reject", "REJECT"],
    DEVIATION_ACCEPT: ["chip-deviation", "DEVIATION"],
    NEED_CLARIFICATION: ["chip-clarify", "CLARIFY"],
    STOPPING: ["chip-screening", "停止中"],
    STOPPED: ["chip-info", "已停止"],
    REQUIREMENT: ["chip-screening", "待分析"],
    SCREENING_FAILED: ["chip-reject", "筛选失败"],
    ANALYSIS_FAILED: ["chip-reject", "分析失败"],
    INFO: ["chip-info", "INFO"],
  };
  const [cls, label] = map[decision] || map.NEED_CLARIFICATION;
  return `<span class="status-chip ${cls}">${label}</span>`;
}

function renderParentSectionLabel(item) {
  const label = getOwnerSectionLabel(item);
  return escapeHtml(label || "-");
}

function renderParentSectionPlainText(item) {
  return getOwnerSectionLabel(item) || "-";
}

function getOwnerSectionLabel(item) {
  const ownerLabel = String(item?.owner_section_label || "").trim();
  if (ownerLabel) {
    return ownerLabel;
  }
  const ownerCode = String(item?.owner_section_code || item?.section_code || item?.parent_section_code || "").trim();
  if (ownerCode) {
    return ownerCode;
  }
  const ownerTitle = String(item?.owner_section_title || item?.section_title || item?.parent_section_title || "").trim();
  if (ownerTitle) {
    return ownerTitle;
  }
  return extractSectionLabelFromAnchor(item?.source_anchor) || String(item?.heading_text || "").trim();
}

function formatAiJobStatus(status) {
  const normalized = String(status || "").trim().toUpperCase();
  const mapping = {
    IDLE: "未启动",
    PENDING: "排队中",
    RUNNING: "运行中",
    CANCEL_REQUESTED: "停止中",
    CANCELLED: "已停止",
    COMPLETED: "已完成",
    FAILED: "失败",
  };
  return mapping[normalized] || normalized || "未启动";
}

function formatAiJobStage(stage) {
  const normalized = String(stage || "").trim().toLowerCase();
  const mapping = {
    queued: "排队",
    starting: "准备上下文",
    screening: "筛选",
    retrieval: "检索证据",
    assessment: "逐条分析",
    cancelling: "停止中",
    cancelled: "已停止",
    completed: "完成",
    failed: "失败",
    ai_analyzing: "逐条分析",
    ai_ready: "完成",
    extraction_locked: "等待 AI 启动",
  };
  return mapping[normalized] || normalized || "待机";
}

function formatAiEventType(eventType) {
  const normalized = String(eventType || "").trim().toLowerCase();
  const mapping = {
    job_created: "任务已创建",
    job_started: "任务已启动",
    job_cancel_requested: "停止请求",
    job_cancelled: "任务已停止",
    requirement_started: "开始处理需求",
    requirement_stage_update: "阶段更新",
    requirement_screening_reasoning: "筛选推理",
    requirement_screening_output: "筛选输出",
    requirement_reasoning: "实时推理",
    requirement_answer_started: "生成结论",
    requirement_output: "结构化输出",
    requirement_completed: "完成需求分析",
    job_completed: "任务完成",
    job_failed: "任务失败",
  };
  return mapping[normalized] || normalized || "状态更新";
}

function getAiEventToneClass(event) {
  const eventType = String(event?.event_type || "").trim().toLowerCase();
  if (eventType === "job_failed") {
    return "is-failed";
  }
  if (eventType === "job_cancel_requested" || eventType === "job_cancelled") {
    return "is-note";
  }
  if (eventType === "job_completed") {
    return "is-completed";
  }
  if (eventType === "requirement_stage_update") {
    return "is-note";
  }
  if (eventType === "requirement_screening_reasoning") {
    return "is-screening";
  }
  if (eventType === "requirement_screening_output" || eventType === "requirement_output" || eventType === "requirement_answer_started") {
    return "is-output";
  }
  if (eventType === "requirement_reasoning") {
    return "is-reasoning";
  }
  return "is-running";
}

function formatEvidenceRelevance(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "-";
  }
  return number.toFixed(3);
}

function renderAiEvidenceList(evidences) {
  if (!Array.isArray(evidences) || !evidences.length) {
    return `<div class="ai-requirement-empty">当前条目暂无可用证据。请结合步骤日志检查检索与回退路径。</div>`;
  }
  return `<div class="ai-evidence-list">${evidences
    .map((evidence) => {
      const sourceName = String(evidence.source_doc_name || "").trim() || "未知知识文档";
      const sourceVersion = String(evidence.source_doc_version || "").trim();
      const location = String(evidence.source_location || "").trim();
      const header = sourceVersion ? `${sourceName} · ${sourceVersion}` : sourceName;
      return `
        <article class="ai-evidence-card">
          <div class="ai-evidence-head">
            <div>
              <div class="ai-evidence-source">${escapeHtml(header)}</div>
              <div class="ai-evidence-location">${escapeHtml(location || "未标注位置")}</div>
            </div>
            <div class="ai-evidence-location">相关度 ${escapeHtml(formatEvidenceRelevance(evidence.relevance))}</div>
          </div>
          <p class="ai-evidence-excerpt">${escapeHtml(String(evidence.excerpt || "").trim() || "无摘录")}</p>
        </article>
      `;
    })
    .join("")}</div>`;
}

function renderAiNormalizationPanel(normalization) {
  const summary = String(normalization?.summary || "").trim();
  const facets = Array.isArray(normalization?.facets) ? normalization.facets : [];
  if (!summary && !facets.length) {
    return "";
  }
  const facetMarkup = facets.length
    ? `<div class="ai-normalization-grid">${facets
        .map((facet) => {
          const label = String(facet?.label || "").trim();
          const value = String(facet?.value || "").trim();
          if (!label || !value) {
            return "";
          }
          return `
            <div class="ai-normalization-item">
              <div class="ai-normalization-label">${escapeHtml(label)}</div>
              <div class="ai-normalization-value">${escapeHtml(value)}</div>
            </div>
          `;
        })
        .join("")}</div>`
    : "";
  return `
    <div class="ai-detail-block">
      <h4>规范化需求</h4>
      ${summary ? `<p class="ai-normalization-summary">${escapeHtml(summary)}</p>` : ""}
      ${facetMarkup}
    </div>
  `;
}

function renderAiRunList(runs) {
  if (!Array.isArray(runs) || !runs.length) {
    return `<div class="ai-requirement-empty">当前条目暂无模型运行记录。</div>`;
  }
  return `<div class="ai-run-list">${runs
    .map((run) => {
      const title = [String(run.mode || "").trim() || "unknown", String(run.model_name || "").trim() || "model"]
        .filter(Boolean)
        .join(" · ");
      const meta = [
        String(run.retrieval_backend || "").trim() || "local",
        String(run.validation_status || "").trim() || "未校验",
        formatTime(run.created_at) || "刚刚",
      ]
        .filter(Boolean)
        .join(" · ");
      const body = String(run.fallback_reason || "").trim() || "无回退，直接生成最终结果。";
      return `
        <article class="ai-run-card">
          <div class="ai-run-head">
            <div class="ai-run-title">${escapeHtml(title)}</div>
            <div class="ai-run-meta">${escapeHtml(meta)}</div>
          </div>
          <p class="ai-run-body">${escapeHtml(body)}</p>
        </article>
      `;
    })
    .join("")}</div>`;
}

function buildSectionLabel(label, code, title) {
  const directLabel = String(label || "").trim();
  if (directLabel) {
    return directLabel;
  }
  return [String(code || "").trim(), String(title || "").trim()].filter(Boolean).join(" ");
}

function formatScope(scope, title) {
  const normalizedTitle = String(title || "").trim();
  if (normalizedTitle) {
    return normalizedTitle;
  }
  const normalized = String(scope || "MAIN").trim().toUpperCase();
  return normalized || "MAIN";
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

function getAnalysisLabel(row) {
  const label = String(row?.analysis_label || "").trim().toUpperCase();
  return label || "REQUIREMENT";
}

function getAssessmentMode(row) {
  return String(row?.assessment_mode || "").trim().toLowerCase();
}

function getAiOutcome(row, assessmentOverride) {
  const analysisLabel = getAnalysisLabel(row);
  if (analysisLabel === "INFO") {
    return "INFO";
  }
  const assessmentMode = getAssessmentMode(assessmentOverride || row);
  if (assessmentMode === "screening_failed") {
    return "SCREENING_FAILED";
  }
  if (assessmentMode === "analysis_failed") {
    return "ANALYSIS_FAILED";
  }
  return String((assessmentOverride || row)?.ai_decision || row?.ai_decision || "").trim().toUpperCase() || "REQUIREMENT";
}

function renderAiAssessmentCell(row) {
  const analysisLabel = getAnalysisLabel(row);
  const outcome = getAiOutcome(row);
  const decisionReason = String(row?.ai_reason || "").trim();
  const analysisBasis = String(row?.analysis_basis || "").trim();
  const detailAction = row?.id
    ? `<button class="ai-inline-action" data-ai-detail="${escapeHtml(String(row.id || ""))}" type="button">查看证据</button>`
    : "";
  if (analysisLabel === "INFO") {
    return `
      <div class="ai-result-card">
        <div class="ai-result-head">
          <span class="ai-result-label">AI 标签</span>
          ${decisionChip("INFO")}
        </div>
        <p class="ai-result-basis">${escapeHtml(analysisBasis || "条目被识别为说明性信息。")}</p>
        ${detailAction}
      </div>
    `;
  }
  if (!outcome || outcome === "REQUIREMENT") {
    if (!analysisBasis) {
      return `<div class="matrix-muted">未运行 AI 分析</div>`;
    }
    return `
      <div class="ai-result-card">
        <div class="ai-result-head">
          <span class="ai-result-label">分析状态</span>
          ${decisionChip("REQUIREMENT")}
        </div>
        <p class="ai-result-basis">${escapeHtml(analysisBasis)}</p>
        ${detailAction}
      </div>
    `;
  }
  if (!decisionReason && !analysisBasis) {
    return `<div class="matrix-muted">未运行 AI 分析</div>`;
  }
  return `
    <div class="ai-result-card">
      <div class="ai-result-head">
        <span class="ai-result-label">判定结果</span>
        ${decisionChip(outcome || "NEED_CLARIFICATION")}
      </div>
      <p class="ai-result-basis">${escapeHtml(decisionReason || analysisBasis || "暂无")}</p>
        ${detailAction}
    </div>
  `;
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
    String(ref.slice_render_mode || "") === "structured" && (headers.length || rows.length) ? "结构化切片" : "切片快照";
  let content = "";
  if (variant === "source" && ref.source_snapshot_url) {
    content = renderTableSnapshot(ref, row, ref.source_snapshot_url, "source table snapshot");
  } else if (String(ref.slice_render_mode || "") === "structured" && (headers.length || rows.length)) {
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
    if (state.reviewDefaultTableVariant === "source" && ref?.source_snapshot_url) {
      return "source";
    }
    if (state.reviewDefaultTableVariant === "slice") {
      return "slice";
    }
    return ref?.source_snapshot_url ? "source" : "slice";
  }
  const preferred = state.tableViewByTableId[tableId];
  if (preferred === "source" || preferred === "slice") {
    if (preferred === "source" && !ref?.source_snapshot_url) {
      return "slice";
    }
      return preferred;
  }
  if (state.reviewDefaultTableVariant === "source" && ref?.source_snapshot_url) {
    return "source";
  }
  if (state.reviewDefaultTableVariant === "slice") {
    return "slice";
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
  const hasHeader = normalizedHeaders.some((cell) => cell);
  const head = hasHeader ? normalizedHeaders.map((cell) => `<th>${escapeHtml(cell)}</th>`).join("") : "";
  const body = normalizedRows
    .map(
      (row) =>
        `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell || ""))}</td>`).join("")}</tr>`
    )
    .join("");
  const thead = hasHeader ? `<thead><tr>${head}</tr></thead>` : "";
  return `<div class="md-table-wrap asset-table-wrap"><table class="md-table asset-table md-table-accurate">${colgroup}${thead}<tbody>${body}</tbody></table></div>`;
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

function isAsciiText(value) {
  return /^[\x00-\x7F]+$/.test(String(value || ""));
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

function uploadWithProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const ct = xhr.getResponseHeader("content-type") || "";
        try {
          resolve(ct.includes("application/json") ? JSON.parse(xhr.responseText) : xhr.responseText);
        } catch (err) {
          reject(new Error(`解析响应失败: ${err.message}`));
        }
      } else {
        reject(new Error(`上传失败(${xhr.status}): ${xhr.responseText}`));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("网络错误，请检查连接")));
    xhr.addEventListener("timeout", () => reject(new Error("上传超时")));
    xhr.timeout = 600000;
    xhr.send(formData);
  });
}

function showUploadProgress(type) {
  const progress = el[`${type}UploadProgress`];
  const fill = el[`${type}UploadProgressFill`];
  const text = el[`${type}UploadProgressText`];
  if (progress) progress.hidden = false;
  if (fill) fill.style.width = "0%";
  if (text) text.textContent = "上传中 0%";
}

function setUploadProgress(type, percent) {
  const fill = el[`${type}UploadProgressFill`];
  const text = el[`${type}UploadProgressText`];
  if (fill) {
    fill.style.width = `${percent}%`;
    fill.classList.remove("upload-progress-indeterminate");
  }
  if (text) text.textContent = `上传中 ${percent}%`;
}

function setUploadProcessing(type, processing) {
  const fill = el[`${type}UploadProgressFill`];
  const text = el[`${type}UploadProgressText`];
  if (fill) {
    if (processing) {
      fill.classList.add("upload-progress-indeterminate");
      fill.style.width = "100%";
    } else {
      fill.classList.remove("upload-progress-indeterminate");
    }
  }
  if (text) text.textContent = processing ? "服务端处理中..." : "上传完成";
}

function hideUploadProgress(type) {
  const progress = el[`${type}UploadProgress`];
  if (progress) progress.hidden = true;
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
