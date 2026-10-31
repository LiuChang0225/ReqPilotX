import { useEffect, useMemo, useRef, useState } from "react";
import { api, asRecord } from "../../shared/api/client";
import type {
  JsonRecord,
  RequirementMatrixSnapshot,
  RequirementSet,
} from "../../shared/api/contracts";
import { compactText, formatDate } from "../../shared/lib/format";
import {
  EmptyState,
  InlineNotice,
  LoadingBlock,
  StatusBadge,
} from "../../shared/ui/Feedback";
import { Icon } from "../../shared/ui/Icon";
import { useUiCopy } from "../../host/HostLocaleContext";
import {
  documentScopeKey,
  requirementSetScopeKey,
  useWorkspace,
} from "../workspace/WorkspaceContext";
import { matrixExportReadiness } from "./exportReadiness";

function setId(set: RequirementSet): string {
  return String(set.id || set.requirement_set_id || "");
}

function jobId(job: JsonRecord): string {
  return String(job.id || job.job_id || "");
}

async function waitForExport(
  projectId: string,
  started: JsonRecord,
  l: (key: string, fallback?: string, params?: Record<string, string | number>) => string,
): Promise<JsonRecord> {
  if (started.download_url) return started;
  if (String(started.status).toUpperCase() === "SUCCEEDED") {
    return asRecord(started.result);
  }
  const id = String(started.job_id || "");
  if (!id) throw new Error(l("exports.missingJobId", "导出任务没有返回可轮询的 job_id。"));
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    const snapshot = await api.exportJob(projectId, id);
    const status = String(snapshot.status || "").toUpperCase();
    if (status === "SUCCEEDED") return asRecord(snapshot.result);
    if (["FAILED", "CANCELLED"].includes(status)) {
      const error = asRecord(snapshot.error);
      throw new Error(
        compactText(error.message, l("exports.jobStatus", "导出任务 {status}。", { status: status.toLowerCase() })),
      );
    }
  }
  throw new Error(l("exports.timeout", "导出任务在三分钟内没有完成，请到历史记录查看。"));
}

export function ExportsPanel() {
  const l = useUiCopy();
  const {
    route,
    updateRoute,
    selectedDocument,
    workspace,
    requirementSets,
    exports,
    loading,
    refreshing,
    busyAction,
    runAction,
    refreshDocument,
    refreshExports,
  } = useWorkspace();
  const sets = requirementSets?.requirement_sets || [];
  const requirementSetsLoading =
    requirementSets === null && (loading || refreshing);
  const activeSetId = String(
    requirementSets?.active_requirement_set_id ||
      workspace?.document?.active_requirement_set_id ||
      "",
  );
  const requestedSetId = route.requirementSetId;
  const selectedSet = sets.find((set) => setId(set) === requestedSetId) || null;
  const selectedSetId = selectedSet ? requestedSetId : "";
  const exportScope = documentScopeKey(route.projectId, route.documentId);
  const exportResultScope = requirementSetScopeKey(
    route.projectId,
    route.documentId,
    selectedSetId,
  );
  const exportFormScope = requirementSetScopeKey(
    route.projectId,
    route.documentId,
    route.requirementSetId,
  );
  const currentExportResultScopeRef = useRef(exportResultScope);
  currentExportResultScopeRef.current = exportResultScope;
  const [exportForm, setExportForm] = useState<{
    scope: string;
    mode: "DRAFT" | "FORMAL";
    label: string;
  }>({ scope: exportFormScope, mode: "DRAFT", label: "" });
  const mode =
    exportForm.scope === exportFormScope ? exportForm.mode : "DRAFT";
  const label = exportForm.scope === exportFormScope ? exportForm.label : "";
  useEffect(() => {
    setExportForm((current) =>
      current.scope === exportFormScope &&
      current.mode === "DRAFT" &&
      !current.label
        ? current
        : { scope: exportFormScope, mode: "DRAFT", label: "" },
    );
  }, [exportFormScope]);
  const [compareSelection, setCompareSelection] = useState({
    scope: exportScope,
    id: "",
  });
  const [lastResultState, setLastResultState] = useState<{
    scope: string;
    result: JsonRecord;
  } | null>(null);
  const [selectedMatrix, setSelectedMatrix] =
    useState<RequirementMatrixSnapshot | null>(null);
  const [matrixLoading, setMatrixLoading] = useState(false);
  const [matrixError, setMatrixError] = useState("");
  const [matrixReloadKey, setMatrixReloadKey] = useState(0);

  useEffect(() => {
    void refreshExports();
  }, [refreshExports]);

  const documentExports = useMemo(
    () =>
      exports.filter(
        (job) =>
          String(job.document_id || job.sor_document_id || "") ===
          route.documentId,
      ),
    [exports, route.documentId],
  );
  const compareId =
    compareSelection.scope === exportScope &&
    documentExports.some((job) => jobId(job) === compareSelection.id)
      ? compareSelection.id
      : "";
  const lastResult =
    lastResultState?.scope === exportResultScope
      ? lastResultState.result
      : null;
  useEffect(() => {
    if (!route.projectId || !route.documentId || !selectedSetId) {
      setSelectedMatrix(null);
      setMatrixError("");
      setMatrixLoading(false);
      return;
    }
    let active = true;
    setMatrixLoading(true);
    setMatrixError("");
    setSelectedMatrix(null);
    void api
      .requirementMatrix(route.projectId, route.documentId, selectedSetId)
      .then((matrix) => {
        if (active) setSelectedMatrix(matrix);
      })
      .catch((cause) => {
        if (!active) return;
        setSelectedMatrix(null);
        setMatrixError(
          cause instanceof Error
            ? cause.message
            : l("exports.matrixReadFailed", "当前版本的统一需求矩阵暂时无法读取。"),
        );
      })
      .finally(() => {
        if (active) setMatrixLoading(false);
      });
    return () => {
      active = false;
    };
  }, [matrixReloadKey, route.documentId, route.projectId, selectedSetId]);

  const setStatus = String(selectedSet?.status || "DRAFT").toUpperCase();
  const {
    targetCount: requirementCount,
    effectiveConclusionCount,
    missingEffectiveConclusionCount,
    effectiveCoverage,
    aiDefaultCount,
    humanOverrideCount,
    humanNativeCount,
    matrixReady,
    previewStable,
    formalReady,
  } = useMemo(
    () => matrixExportReadiness(selectedMatrix, selectedSet),
    [selectedMatrix, selectedSet],
  );
  const formalBlockers = useMemo(() => {
    if (!selectedSetId) return [];
    const blockers: string[] = [];
    if (setStatus !== "LOCKED") {
      blockers.push(l("exports.blockerUnlocked", "当前需求版本尚未锁定。请先锁定确认清单。"));
    }
    if (!previewStable) {
      blockers.push(
        l("exports.blockerPreview", "当前需求版本缺少稳定解析预览证明。请返回解析核对，重新生成并锁定清单。"),
      );
    }
    if (missingEffectiveConclusionCount > 0) {
      blockers.push(
        l("exports.blockerCoverage", "统一需求矩阵有效结论覆盖 {coverage}%，缺少有效结论 {missing} 条。请在需求分析中完成或人工补充这些结论。", { coverage: effectiveCoverage, missing: missingEffectiveConclusionCount }),
      );
    }
    if (!matrixReady) {
      blockers.push(
        selectedMatrix?.state === "REQUIRES_REEXTRACTION"
          ? l("exports.blockerReextract", "统一需求矩阵尚未 READY：当前版本缺少可证明的冻结结构，需要重新提取并生成新确认清单。")
          : l("exports.blockerNotReady", "统一需求矩阵尚未 READY。请等待矩阵生成完成或重试读取。"),
      );
    }
    return blockers;
  }, [
    matrixReady,
    effectiveCoverage,
    missingEffectiveConclusionCount,
    previewStable,
    selectedMatrix?.state,
    selectedSetId,
    setStatus,
  ]);

  const createExport = async () => {
    if (
      !selectedDocument ||
      !selectedSetId ||
      !matrixReady ||
      (mode === "FORMAL" && !formalReady)
    ) return;
    const result = await runAction(
      `创建${mode === "FORMAL" ? "正式" : "草稿"}导出`,
      async () => {
        const started = await api.createExport(route.projectId, {
          version_label: label.trim() || null,
          compare_export_id: compareId || null,
          document_id: route.documentId,
          requirement_set_id: selectedSetId,
          mode,
        });
        return waitForExport(route.projectId, started, l);
      },
      { refresh: "none", scope: "requirementSet" },
    );
    if (!result) return;
    if (currentExportResultScopeRef.current !== exportResultScope) return;
    setLastResultState({ scope: exportResultScope, result });
    setExportForm((current) =>
      current.scope === exportFormScope
        ? { ...current, label: "" }
        : current,
    );
    await refreshExports();
  };

  return (
    <section className="management-page exports-page">
      <header className="page-command-header">
        <div>
          <p className="section-label">{l("矩阵导出")}</p>
          <h1>{l("需求版本与矩阵")}</h1>
          <p>{l("选择需求版本、核对有效结论覆盖，再创建可追溯矩阵。")}</p>
        </div>
        <button
          className="button secondary"
          type="button"
          onClick={() => void refreshExports()}
        >
          <Icon name="refresh" />
          {l("刷新记录")}
        </button>
      </header>

      {!selectedDocument ? (
        <EmptyState
          icon="export"
          title={l("先选择客户来源")}
          description={l("exports.bindNote", "每个导出都严格绑定一份来源文档与一个需求版本。")}
          action={
            <button
              className="button primary"
              type="button"
              onClick={() => updateRoute({ task: "projects" })}
            >
              {l("打开项目管理")}
            </button>
          }
        />
      ) : requirementSetsLoading ? (
        <LoadingBlock label={l("exports.loadingList", "正在读取确认清单")} />
      ) : requirementSets === null ? (
        <EmptyState
          icon="document"
          title={l("确认清单暂不可读取")}
          description={l("exports.listUnavailable", "当前文档的确认清单尚未返回，请重试读取。")}
          action={
            <button
              className="button primary"
              type="button"
              onClick={() => void refreshDocument()}
            >
              {l("重试读取")}
            </button>
          }
        />
      ) : !sets.length ? (
        <EmptyState
          icon="document"
          title={l("尚无确认清单")}
          description={l("exports.startFromExtraction", "请先从解析核对开始，在需求提取中完成结构整理并生成确认清单。")}
          action={
            <button
              className="button primary"
              type="button"
              onClick={() =>
                updateRoute({
                  task: "extraction",
                  mode: "organize",
                  checkpoint: "diagnose",
                  requirementSetId: "",
                })
              }
            >
              {l("前往需求提取")}
            </button>
          }
        />
      ) : (
        <>
          <div className="exports-overview">
            <section className="requirement-set-ledger">
              <header>
                <div>
                  <p className="section-label">{l("需求版本")}</p>
                  <h2>{l("版本台账")}</h2>
                </div>
                <span>{sets.length}</span>
              </header>
              {sets.length ? (
                <div className="set-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{l("版本")}</th>
                        <th>{l("状态")}</th>
                        <th>{l("AI 分析")}</th>
                        <th>{l("有效结论")}</th>
                        <th>{l("创建时间")}</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {sets.map((set) => {
                        const id = setId(set);
                        const matrixSelected = id === selectedSetId;
                        const active = id === activeSetId;
                        return (
                          <tr
                            key={id}
                            className={
                              id === selectedSetId ? "is-selected" : ""
                            }
                          >
                            <td>
                              <button
                                className="set-version-button"
                                type="button"
                                aria-pressed={matrixSelected}
                                onClick={() =>
                                  updateRoute(
                                    { requirementSetId: id },
                                    { replace: true },
                                  )
                                }
                              >
                                v
                                {String(
                                  set.version ??
                                    set.version_number ??
                                    "—",
                                )}
                              </button>
                            </td>
                            <td>
                              <StatusBadge
                                tone={
                                  String(set.status).toUpperCase() === "LOCKED"
                                    ? "success"
                                    : "warning"
                                }
                              >
                                {String(set.status || "DRAFT")}
                              </StatusBadge>
                            </td>
                            <td>
                              {compactText(
                                set.latest_ai_run_status ??
                                  set.ai_status ??
                                  set.ai_run_status,
                                l("exports.notRun", "未运行"),
                              )}
                            </td>
                            <td>
                              {matrixSelected && selectedMatrix
                                ? `${effectiveConclusionCount}/${requirementCount} · ${effectiveCoverage}%`
                                : matrixSelected && matrixLoading
                                  ? l("exports.reading", "读取中…")
                                  : l("exports.selectToView", "选择查看")}
                            </td>
                            <td>{formatDate(set.created_at)}</td>
                            <td>
                              {active ? (
                                <span>{l("已激活")}</span>
                              ) : (
                                <button
                                  className="text-button"
                                  type="button"
                                  onClick={() => {
                                    void runAction(
                                      "切换当前需求版本",
                                      () =>
                                        api.activateRequirementSet(
                                          route.projectId,
                                          route.documentId,
                                          id,
                                        ),
                                      { refresh: "document" },
                                    );
                                  }}
                                >
                                  {l("设为当前")}
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState
                  icon="document"
                  title={l("尚无确认清单")}
                  description={l("exports.generateListFirst", "在需求提取中生成确认清单后才可创建矩阵。")}
                />
              )}
            </section>

            <aside className="export-compose">
              {!selectedSetId ? (
                <EmptyState
                  icon="matrix"
                  title={l("选择需求版本")}
                  description={l("exports.chooseFromLedger", "请从左侧版本台账选择要核对和导出的确认清单。")}
                  action={
                    activeSetId ? (
                      <button
                        className="button primary"
                        type="button"
                        onClick={() =>
                          updateRoute(
                            { requirementSetId: activeSetId },
                            { replace: true },
                          )
                        }
                      >
                        {l("选择当前版本")}
                      </button>
                    ) : undefined
                  }
                />
              ) : (
                <>
              <p className="section-label">{l("创建导出")}</p>
              <h2>{l("创建导出")}</h2>
              <div className="export-readiness-meter">
                <div>
                  <span>{l("有效结论覆盖")}</span>
                  <strong>{effectiveCoverage}%</strong>
                </div>
                <div className="run-progress">
                  <span style={{ width: `${effectiveCoverage}%` }} />
                </div>
                <small>
                  {matrixLoading
                    ? l("exports.checkingMatrix", "正在按统一需求矩阵口径核对")
                    : l("exports.coverageDetail", "{effective}/{total} 条需分析内容已有有效结论；当前终态 AI 结论默认采用，人工修改为覆盖例外", { effective: effectiveConclusionCount, total: requirementCount })}
                </small>
                {!matrixLoading && selectedMatrix && (
                  <small>
                        AI 默认 {aiDefaultCount} · 人工修改 {humanOverrideCount}{" "}
                        · 人工录入 {humanNativeCount}
                  </small>
                )}
              </div>
              {matrixError && (
                <InlineNotice title={l("统一需求矩阵读取失败")} tone="warning">
                  {matrixError} DRAFT 与 FORMAL 均不会绕过当前版本矩阵。
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => setMatrixReloadKey((current) => current + 1)}
                  >
                    {l("重试读取")}
                  </button>
                </InlineNotice>
              )}
              {!matrixLoading &&
                !matrixError &&
                selectedSetId &&
                selectedMatrix?.state !== "READY" && (
                  <InlineNotice title={l("统一需求矩阵尚未就绪")} tone="warning">
                    {selectedMatrix?.state === "REQUIRES_REEXTRACTION"
                      ? l("exports.matrixReextract", "当前版本缺少可证明的冻结结构，请重新提取并生成新版本。")
                      : l("exports.matrixIncomplete", "当前版本矩阵尚未完成，DRAFT 与 FORMAL 均暂不可导出。")}
                    {selectedMatrix?.state === "REQUIRES_REEXTRACTION" ? (
                      <button
                        className="text-button"
                        type="button"
                        onClick={() =>
                              updateRoute({
                                task: "extraction",
                                mode: "organize",
                                checkpoint: "diagnose",
                                requirementSetId: "",
                              })
                        }
                      >
                        {l("返回需求提取")}
                      </button>
                    ) : (
                      <button
                        className="text-button"
                        type="button"
                        onClick={() => setMatrixReloadKey((current) => current + 1)}
                      >
                        {l("重试读取")}
                      </button>
                    )}
                  </InlineNotice>
                )}
              <label>
                <span>{l("需求版本")}</span>
                <select
                  name="export-requirement-set"
                  autoComplete="off"
                  value={selectedSetId}
                  onChange={(event) =>
                    updateRoute(
                      { requirementSetId: event.target.value },
                      { replace: true },
                    )
                  }
                >
                  <option value="">{l("选择需求版本")}</option>
                  {sets.map((set) => (
                    <option key={setId(set)} value={setId(set)}>
                      v{String(set.version ?? set.version_number ?? "—")} ·{" "}
                      {String(set.status || "DRAFT")}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset className="export-mode">
                <legend>{l("导出模式")}</legend>
                <label>
                  <input
                    type="radio"
                    name="export-mode"
                    checked={mode === "DRAFT"}
                    onChange={() =>
                      setExportForm((current) => ({
                        scope: exportFormScope,
                        mode: "DRAFT",
                        label:
                          current.scope === exportFormScope
                            ? current.label
                            : "",
                      }))
                    }
                  />
                  <span>
                    <strong>DRAFT</strong>
                    <small>{l("允许未满覆盖，用于内部校对")}</small>
                  </span>
                </label>
                <label>
                  <input
                    type="radio"
                    name="export-mode"
                    checked={mode === "FORMAL"}
                    onChange={() =>
                      setExportForm((current) => ({
                        scope: exportFormScope,
                        mode: "FORMAL",
                        label:
                          current.scope === exportFormScope
                            ? current.label
                            : "",
                      }))
                    }
                  />
                  <span>
                    <strong>FORMAL</strong>
                    <small>{l("要求锁定版本与 100% 终态有效结论")}</small>
                  </span>
                </label>
              </fieldset>
              <label>
                <span>{l("版本标识")}</span>
                <input
                  name="export-version-label"
                  autoComplete="off"
                  value={label}
                  maxLength={64}
                  onChange={(event) =>
                    setExportForm({
                      scope: exportFormScope,
                      mode,
                      label: event.target.value,
                    })
                  }
                  placeholder={l("例如 r33…")}
                />
              </label>
              <label>
                <span>{l("对比版本")}</span>
                <select
                  name="export-compare-version"
                  autoComplete="off"
                  value={compareId}
                      onChange={(event) =>
                        setCompareSelection({
                          scope: exportScope,
                          id: event.target.value,
                        })
                      }
                >
                  <option value="">{l("不做版本对比")}</option>
                  {documentExports.map((job) => (
                    <option key={jobId(job)} value={jobId(job)}>
                      {compactText(
                        job.version_label ?? job.file_name,
                        jobId(job).slice(0, 10),
                      )}
                    </option>
                  ))}
                </select>
              </label>
                  {mode === "FORMAL" && !matrixLoading && !formalReady && (
                <InlineNotice title={l("FORMAL 尚未就绪")} tone="warning">
                      <ul aria-label={l("FORMAL 导出阻断项")}>
                        {formalBlockers.map((blocker) => (
                          <li key={blocker}>{blocker}</li>
                        ))}
                      </ul>
                      <p>
                        {l("只有“可见 + 需分析 +")}
                        {l("可分析”的内容计入有效结论分母；DRAFT")}
                      {l("导出仍可用于内部校对。")}
                      </p>
                </InlineNotice>
              )}
              <button
                className="button primary full"
                type="button"
                disabled={
                  !selectedSetId ||
                  Boolean(busyAction) ||
                  matrixLoading ||
                  !matrixReady ||
                  (mode === "FORMAL" && !formalReady)
                }
                onClick={() => void createExport()}
              >
                <Icon name="download" />
                {l("导出需求矩阵")}
              </button>
              {lastResult && (
                <a
                  className="button secondary full"
                  href={String(
                    lastResult.download_url ||
                      (lastResult.file_name
                        ? `/api/exports/${encodeURIComponent(String(lastResult.file_name))}`
                        : "#"),
                  )}
                  target="_blank"
                  rel="noreferrer"
                >
                  {l("exports.openLatest", "打开 {name}", { name: compactText(lastResult.file_name, l("exports.latestExport", "最新导出")) })}
                </a>
                  )}
                </>
              )}
            </aside>
          </div>

          <section className="export-history-panel">
            <header>
              <div>
                <p className="section-label">{l("导出记录")}</p>
                <h2>{l("导出记录")}</h2>
              </div>
              <span>{documentExports.length}</span>
            </header>
            <div className="export-history-list">
              {documentExports.length ? (
                documentExports.map((job) => {
                  const fileName = String(job.file_name || "");
                  const downloadUrl = String(
                    job.download_url ||
                      (fileName
                        ? `/api/exports/${encodeURIComponent(fileName)}`
                        : ""),
                  );
                  const rawCoverage = Number(
                    job.human_coverage ?? job.human_coverage_rate ?? 0,
                  );
                  const jobCoverage = Math.round(
                    rawCoverage * (rawCoverage <= 1 ? 100 : 1),
                  );
                  return (
                    <article key={jobId(job)}>
                      <div className="export-file-icon">
                        <Icon name="matrix" />
                      </div>
                      <div>
                        <strong>
                          {compactText(
                            job.version_label ?? fileName,
                            jobId(job).slice(0, 12),
                          )}
                        </strong>
                        <small>{fileName || l("exports.generatingFile", "正在生成文件")}</small>
                      </div>
                      <StatusBadge
                        tone={
                          String(job.status || "SUCCEEDED").toUpperCase() ===
                          "SUCCEEDED"
                            ? "success"
                            : "warning"
                        }
                      >
                        {String(
                          job.export_mode || job.mode || job.status || "DRAFT",
                        )}
                      </StatusBadge>
                      <span>
                        {l("exports.setVersion", "Set v{version} · 人工修改覆盖 {coverage}%", { version: String(job.requirement_set_version || "—"), coverage: jobCoverage })}
                      </span>
                      <time>{formatDate(job.created_at)}</time>
                      {downloadUrl ? (
                        <a
                          className="button ghost compact"
                          href={downloadUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <Icon name="download" />
                          {l("下载")}
                        </a>
                      ) : (
                        <span>—</span>
                      )}
                    </article>
                  );
                })
              ) : (
                <EmptyState
                  icon="export"
                  title={l("还没有导出记录")}
                  description={l("exports.firstExportHint", "选择需求版本并创建第一份 DRAFT 或 FORMAL 矩阵。")}
                />
              )}
            </div>
          </section>
        </>
      )}
    </section>
  );
}
