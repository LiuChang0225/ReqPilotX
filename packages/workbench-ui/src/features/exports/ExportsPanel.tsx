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
): Promise<JsonRecord> {
  if (started.download_url) return started;
  if (String(started.status).toUpperCase() === "SUCCEEDED") {
    return asRecord(started.result);
  }
  const id = String(started.job_id || "");
  if (!id) throw new Error("导出任务没有返回可轮询的 job_id。");
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    const snapshot = await api.exportJob(projectId, id);
    const status = String(snapshot.status || "").toUpperCase();
    if (status === "SUCCEEDED") return asRecord(snapshot.result);
    if (["FAILED", "CANCELLED"].includes(status)) {
      const error = asRecord(snapshot.error);
      throw new Error(
        compactText(error.message, `导出任务 ${status.toLowerCase()}。`),
      );
    }
  }
  throw new Error("导出任务在三分钟内没有完成，请到历史记录查看。");
}

export function ExportsPanel() {
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
            : "当前版本的统一需求矩阵暂时无法读取。",
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
      blockers.push("当前需求版本尚未锁定。请先锁定确认清单。");
    }
    if (!previewStable) {
      blockers.push(
        "当前需求版本缺少稳定解析预览证明。请返回解析核对，重新生成并锁定清单。",
      );
    }
    if (missingEffectiveConclusionCount > 0) {
      blockers.push(
        `统一需求矩阵有效结论覆盖 ${effectiveCoverage}%，缺少有效结论 ${missingEffectiveConclusionCount} 条。请在需求分析中完成或人工补充这些结论。`,
      );
    }
    if (!matrixReady) {
      blockers.push(
        selectedMatrix?.state === "REQUIRES_REEXTRACTION"
          ? "统一需求矩阵尚未 READY：当前版本缺少可证明的冻结结构，需要重新提取并生成新确认清单。"
          : "统一需求矩阵尚未 READY。请等待矩阵生成完成或重试读取。",
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
        return waitForExport(route.projectId, started);
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
          <p className="section-label">矩阵导出</p>
          <h1>需求版本与矩阵</h1>
          <p>选择需求版本、核对有效结论覆盖，再创建可追溯矩阵。</p>
        </div>
        <button
          className="button secondary"
          type="button"
          onClick={() => void refreshExports()}
        >
          <Icon name="refresh" />
          刷新记录
        </button>
      </header>

      {!selectedDocument ? (
        <EmptyState
          icon="export"
          title="先选择客户来源"
          description="每个导出都严格绑定一份来源文档与一个需求版本。"
          action={
            <button
              className="button primary"
              type="button"
              onClick={() => updateRoute({ task: "projects" })}
            >
              打开项目管理
            </button>
          }
        />
      ) : requirementSetsLoading ? (
        <LoadingBlock label="正在读取确认清单" />
      ) : requirementSets === null ? (
        <EmptyState
          icon="document"
          title="确认清单暂不可读取"
          description="当前文档的确认清单尚未返回，请重试读取。"
          action={
            <button
              className="button primary"
              type="button"
              onClick={() => void refreshDocument()}
            >
              重试读取
            </button>
          }
        />
      ) : !sets.length ? (
        <EmptyState
          icon="document"
          title="尚无确认清单"
          description="请先从解析核对开始，在需求提取中完成结构整理并生成确认清单。"
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
              前往需求提取
            </button>
          }
        />
      ) : (
        <>
          <div className="exports-overview">
            <section className="requirement-set-ledger">
              <header>
                <div>
                  <p className="section-label">需求版本</p>
                  <h2>版本台账</h2>
                </div>
                <span>{sets.length}</span>
              </header>
              {sets.length ? (
                <div className="set-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>版本</th>
                        <th>状态</th>
                        <th>AI 分析</th>
                        <th>有效结论</th>
                        <th>创建时间</th>
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
                                "未运行",
                              )}
                            </td>
                            <td>
                              {matrixSelected && selectedMatrix
                                ? `${effectiveConclusionCount}/${requirementCount} · ${effectiveCoverage}%`
                                : matrixSelected && matrixLoading
                                  ? "读取中…"
                                  : "选择查看"}
                            </td>
                            <td>{formatDate(set.created_at)}</td>
                            <td>
                              {active ? (
                                <span>已激活</span>
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
                                  设为当前
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
                  title="尚无确认清单"
                  description="在需求提取中生成确认清单后才可创建矩阵。"
                />
              )}
            </section>

            <aside className="export-compose">
              {!selectedSetId ? (
                <EmptyState
                  icon="matrix"
                  title="选择需求版本"
                  description="请从左侧版本台账选择要核对和导出的确认清单。"
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
                        选择当前版本
                      </button>
                    ) : undefined
                  }
                />
              ) : (
                <>
              <p className="section-label">创建导出</p>
              <h2>创建导出</h2>
              <div className="export-readiness-meter">
                <div>
                  <span>有效结论覆盖</span>
                  <strong>{effectiveCoverage}%</strong>
                </div>
                <div className="run-progress">
                  <span style={{ width: `${effectiveCoverage}%` }} />
                </div>
                <small>
                  {matrixLoading
                    ? "正在按统一需求矩阵口径核对"
                    : `${effectiveConclusionCount}/${requirementCount} 条需分析内容已有有效结论；当前终态 AI 结论默认采用，人工修改为覆盖例外`}
                </small>
                {!matrixLoading && selectedMatrix && (
                  <small>
                        AI 默认 {aiDefaultCount} · 人工修改 {humanOverrideCount}{" "}
                        · 人工录入 {humanNativeCount}
                  </small>
                )}
              </div>
              {matrixError && (
                <InlineNotice title="统一需求矩阵读取失败" tone="warning">
                  {matrixError} DRAFT 与 FORMAL 均不会绕过当前版本矩阵。
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => setMatrixReloadKey((current) => current + 1)}
                  >
                    重试读取
                  </button>
                </InlineNotice>
              )}
              {!matrixLoading &&
                !matrixError &&
                selectedSetId &&
                selectedMatrix?.state !== "READY" && (
                  <InlineNotice title="统一需求矩阵尚未就绪" tone="warning">
                    {selectedMatrix?.state === "REQUIRES_REEXTRACTION"
                      ? "当前版本缺少可证明的冻结结构，请重新提取并生成新版本。"
                      : "当前版本矩阵尚未完成，DRAFT 与 FORMAL 均暂不可导出。"}
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
                        返回需求提取
                      </button>
                    ) : (
                      <button
                        className="text-button"
                        type="button"
                        onClick={() => setMatrixReloadKey((current) => current + 1)}
                      >
                        重试读取
                      </button>
                    )}
                  </InlineNotice>
                )}
              <label>
                <span>需求版本</span>
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
                  <option value="">选择需求版本</option>
                  {sets.map((set) => (
                    <option key={setId(set)} value={setId(set)}>
                      v{String(set.version ?? set.version_number ?? "—")} ·{" "}
                      {String(set.status || "DRAFT")}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset className="export-mode">
                <legend>导出模式</legend>
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
                    <small>允许未满覆盖，用于内部校对</small>
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
                    <small>要求锁定版本与 100% 终态有效结论</small>
                  </span>
                </label>
              </fieldset>
              <label>
                <span>版本标识</span>
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
                  placeholder="例如 r33…"
                />
              </label>
              <label>
                <span>对比版本</span>
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
                  <option value="">不做版本对比</option>
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
                <InlineNotice title="FORMAL 尚未就绪" tone="warning">
                      <ul aria-label="FORMAL 导出阻断项">
                        {formalBlockers.map((blocker) => (
                          <li key={blocker}>{blocker}</li>
                        ))}
                      </ul>
                      <p>
                        只有“可见 + 需分析 +
                        可分析”的内容计入有效结论分母；DRAFT
                      导出仍可用于内部校对。
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
                导出需求矩阵
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
                  打开 {compactText(lastResult.file_name, "最新导出")}
                </a>
                  )}
                </>
              )}
            </aside>
          </div>

          <section className="export-history-panel">
            <header>
              <div>
                <p className="section-label">导出记录</p>
                <h2>导出记录</h2>
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
                        <small>{fileName || "正在生成文件"}</small>
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
                        Set v
                        {String(job.requirement_set_version || "—")} ·{" "}
                        人工修改覆盖 {jobCoverage}%
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
                          下载
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
                  title="还没有导出记录"
                  description="选择需求版本并创建第一份 DRAFT 或 FORMAL 矩阵。"
                />
              )}
            </div>
          </section>
        </>
      )}
    </section>
  );
}
