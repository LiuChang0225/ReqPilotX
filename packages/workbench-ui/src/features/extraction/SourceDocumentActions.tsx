import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, request } from "../../shared/api/client";
import { Icon } from "../../shared/ui/Icon";
import { useDocumentAgent } from "../workspace/DocumentAgentContext";
import { SourceItemAgentLoop, type AgentLoopTask } from "./SourceItemAgentLoop";
import { isSaasAccountMode } from "../../shared/api/saasSession";
import { SAAS_KIT_UNAVAILABLE_MESSAGE, useSaasNativeAgentAvailability } from "./saasAgentAvailability";

export type SourceGraphJob = { job_id: string; status: string; kind?: string; error_message?: string;
  progress?: { message?: string }; result?: { status?: string; source_item_job_ids?: string[] } };
type Job = SourceGraphJob;
export type SourceDocumentActivity = { projectId: string; documentId: string; sourceSha256: string;
  jobs: Job[]; checking: boolean; statusUnavailable: boolean; submitting: boolean };
const EMPTY_JOBS: Job[] = [];
export const sourceGraphJobActive = (job: Job) => ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(job.status);
type Capture = { source_sha256: string; elapsed_seconds: number; cache_reused: boolean;
  capture_counts: Record<string, number>; page_facts: unknown[]; gaps: { code: string; message: string; pages?: number[] }[];
  structure_proposal: { nodes: unknown[] } | null };

export async function startSourceDocumentGraph(projectId: string, documentId: string, sourceSha256: string, mapArtifactId: string) {
  if (mapArtifactId && isSaasAccountMode()) throw new Error(SAAS_KIT_UNAVAILABLE_MESSAGE);
  const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure`;
  if (!mapArtifactId) {
    const task = await request<AgentLoopTask>(`${root}/delivery/extraction-loop`, { method: "POST", body: JSON.stringify({
      command_id: `source-document:${crypto.randomUUID()}`, expected_source_sha256: sourceSha256, origin: "USER_REQUEST",
      require_map_confirmation: false,
      goal: "完整阅读当前已上传的客户原文件，自主读取文字、原表和图片，形成来源忠实的文档结构与原文条目，按当前规则分类并作有必要的显式需求拆分；先保存可核对的文档结构，继续形成条目和分类；封面目录整体保留，不逐项建立SRC。仅有项目真实内部依据时做符合性分析，无依据跳过该步骤，最终导出同一版本的完整Matrix草稿并保存到工作台。自主自查并修正实际问题，保留全部原文、来源和未决项。"
    }) });
    return { job_id: task.job_id, status: task.job_status, kind: "agent.source-item.extract-loop" } as Job & { jobs?: Job[] };
  }
  return request<Job & { jobs?: Job[] }>(`${root}/requirement-baseline/all-runs/start`,
    { method: "POST", body: JSON.stringify({ command_id: `source-document:${crypto.randomUUID()}`,
      expected_source_sha256: sourceSha256,
      expected_artifact_id: mapArtifactId }) });
}

export function SourceDocumentActions({ projectId, documentId, sourceSha256, mapArtifactId, onUpdated, externalJobs = EMPTY_JOBS, ready = true, onActivityChange, collaborationRequest = 0 }: {
  projectId: string; documentId: string; sourceSha256: string; mapArtifactId: string; onUpdated: () => void;
  externalJobs?: Job[]; ready?: boolean; onActivityChange?: (activity: SourceDocumentActivity) => void; collaborationRequest?: number;
}) {
  const documentAgent = useDocumentAgent();
  const availability = useSaasNativeAgentAvailability();
  const kitUnavailable = Boolean(mapArtifactId && isSaasAccountMode());
  const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}`;
  const key = `${root}:${sourceSha256}`;
  const current = useRef(key); current.current = key;
  const update = useRef(onUpdated); update.current = onUpdated;
  const [jobState, setJobState] = useState<{ key: string; jobs: Job[]; checking: boolean; unavailable: boolean }>({ key, jobs: [], checking: true, unavailable: false });
  const jobs = jobState.key === key ? jobState.jobs : EMPTY_JOBS;
  const checking = jobState.key !== key || jobState.checking;
  const statusUnavailable = jobState.key === key && jobState.unavailable;
  const setJobs = useCallback((next: Job[]) => setJobState({ key, jobs: next, checking: false, unavailable: false }), [key]);
  const receiveExtractionTask = useCallback((task: AgentLoopTask) => {
    if (task.source_sha256 && task.source_sha256 !== sourceSha256) return;
    setJobs([{ job_id: task.job_id, status: task.job_status, kind: "agent.source-item.extract-loop", progress: task.progress, error_message: task.error_message, result: task.result }]);
  }, [setJobs, sourceSha256]);
  const activityChanged = useRef(onActivityChange); activityChanged.current = onActivityChange;
  const [retry, setRetry] = useState(0);
  const [intakeRequest, setIntakeRequest] = useState(0);
  const [pending, setPending] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [capture, setCapture] = useState<Capture | null>(null);
  useEffect(() => { if (externalJobs.length) setJobs(externalJobs); }, [externalJobs, setJobs]);
  useEffect(() => {
    setJobState({ key, jobs: [], checking: true, unavailable: false }); setNotice(""); setError(""); setCapture(null); setPending("");
    if (!sourceSha256) return;
    const controller = new AbortController();
    const acceptDiscoveredJobs = (next: Job[]) => setJobState(previous => previous.key === key
      && previous.jobs.some(job => job.kind === "agent.source-item.extract-loop" && sourceGraphJobActive(job))
      ? previous : { key, jobs: next, checking: false, unavailable: false });
    void (async () => {
      try {
        const [latest, legacy] = await Promise.allSettled([
          request<AgentLoopTask | null>(`${root}/agent-structure/delivery/extraction-loop/latest`, { signal: controller.signal }),
          request<Job>(`${root}/agent-structure/document-map/status`, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        if (latest.status === "rejected" && !mapArtifactId && !(latest.reason instanceof ApiError && latest.reason.status === 404)) throw latest.reason;
        const task = latest.status === "fulfilled" ? latest.value : null;
        if (task?.job_id && (!task.source_sha256 || task.source_sha256 === sourceSha256)
          && (!mapArtifactId || sourceGraphJobActive({ job_id: task.job_id, status: task.job_status }))) {
          acceptDiscoveredJobs([{ job_id: task.job_id, status: task.job_status, kind: "agent.source-item.extract-loop", progress: task.progress, error_message: task.error_message, result: task.result }]);
          return;
        }
        const job = legacy.status === "fulfilled" ? legacy.value : null;
        const next = job?.job_id && sourceGraphJobActive(job) ? [job]
          : job?.result?.source_item_job_ids?.length ? await Promise.all(job.result.source_item_job_ids.map(id => request<Job>(
            `${root}/agent-structure/delivery/jobs/${encodeURIComponent(id)}`, { signal: controller.signal }))) : [];
        if (!controller.signal.aborted) acceptDiscoveredJobs(next);
      } catch {
        if (!controller.signal.aborted) setJobState(previous => previous.key === key && previous.jobs.some(sourceGraphJobActive)
          ? previous : { key, jobs: [], checking: false, unavailable: true });
      }
    })();
    return () => controller.abort();
  }, [key, root, sourceSha256, mapArtifactId, retry, setJobs]);
  const active = jobs.some(sourceGraphJobActive);
  useEffect(() => { activityChanged.current?.({ projectId, documentId, sourceSha256, jobs, checking, statusUnavailable, submitting: pending === "agent" }); },
    [projectId, documentId, sourceSha256, jobs, checking, statusUnavailable, pending]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void Promise.all(jobs.map(job => ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(job.status)
        ? request<Job>(`${root}/agent-structure/delivery/jobs/${encodeURIComponent(job.job_id)}`, { signal: controller.signal }).then(next => ({ ...job, ...next }))
        : Promise.resolve(job))).then(async next => {
        if (controller.signal.aborted) return;
        const completed = next.filter(job => job.status !== jobs.find(old => old.job_id === job.job_id)?.status && job.status === "SUCCEEDED");
        const childIds = completed.flatMap(job => job.result?.source_item_job_ids || []).filter(id => !next.some(job => job.job_id === id));
        if (childIds.length) next.push(...await Promise.all(childIds.map(id => request<Job>(`${root}/agent-structure/delivery/jobs/${encodeURIComponent(id)}`, { signal: controller.signal }))));
        if (controller.signal.aborted) return;
        setJobs(next);
        if (completed.length) update.current();
        if (!next.some(job => ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(job.status))) {
          setNotice(next.some(job => job.status !== "SUCCEEDED") ? "本次仍有未完成的范围，已完成结果保留。" : "本次任务已结束，可在原文结构与原文需求清单中核对结果。");
          update.current();
        }
      }).catch(cause => { if (!controller.signal.aborted) setError(`任务状态暂时无法读取：${cause.message}`); });
    }, 3000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active, jobs, root]);

  const start = async () => {
    if (kitUnavailable || !availability.ready) return;
    if (pending || active || checking || statusUnavailable || !sourceSha256) return;
    if (!mapArtifactId) { setIntakeRequest(value => value + 1); return; }
    const started = key; setPending("agent"); setError("");
    try {
      const result = await startSourceDocumentGraph(projectId, documentId, sourceSha256, mapArtifactId);
      if (current.current !== started) return;
      setJobs(result.jobs || [result]);
      setNotice(result.jobs?.length === 0 ? "当前原文范围已经处理，可继续核对或分析。" : "任务已保存，Agent 将继续处理当前文档。关闭页面后任务仍可接续。");
      update.current();
    } catch (cause) { if (current.current === started) setError(cause instanceof Error ? cause.message : "任务未能启动。"); }
    finally { if (current.current === started) setPending(""); }
  };
  const fast = async () => {
    const started = key; setPending("fast"); setError("");
    try {
      const value = await request<Capture>(`${root}/source-capture/fast`, { method: "POST", body: JSON.stringify({ expected_source_sha256: sourceSha256 }) });
      if (current.current === started && value.source_sha256 === sourceSha256) setCapture(value);
    } catch (cause) { if (current.current === started) setError(cause instanceof Error ? cause.message : "原文读取未完成。"); }
    finally { if (current.current === started) setPending(""); }
  };
  const downloadCapture = () => {
    if (!capture) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(capture, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "source-capture.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const actions = (
    <div className="source-item-delivery-actions">
      <button className="button primary compact" disabled={kitUnavailable || !availability.ready || !ready || !sourceSha256 || !!pending || active || checking || statusUnavailable} title={kitUnavailable ? SAAS_KIT_UNAVAILABLE_MESSAGE : undefined} onClick={() => void start()}>
        <Icon name="play" size={14} />{pending === "agent" || active ? "Agent 处理中…" : checking ? "正在同步任务…" : mapArtifactId ? "补全原文条目" : "Agent 提取与分析"}</button>
      <button className="button secondary compact" disabled={!sourceSha256 || !!pending} onClick={() => void fast()}>
        {pending === "fast" ? "正在读取…" : "快速读取 · 无 AI"}</button>
      <span role="status">{jobs.find(job => job.status === "RUNNING")?.progress?.message || notice}</span>
      {kitUnavailable ? <p role="status">{SAAS_KIT_UNAVAILABLE_MESSAGE}</p> : availability.message && <p role="status">{availability.message} <button type="button" className="button ghost compact" onClick={availability.refresh}>刷新 Agent 配置</button></p>}
      {!mapArtifactId && <SourceItemAgentLoop projectId={projectId} documentId={documentId} sourceSha256={sourceSha256}
        baselineSha256="" selectedSourceItemId="" mode="extract" onUpdated={onUpdated} openRequest={collaborationRequest + intakeRequest} onTaskChange={receiveExtractionTask}
        initialGoal="完整阅读当前客户文档，先给出可核对结构，再提取原文条目和分类，按已定义粒度进行必要拆分，最终保存同版本Matrix草稿。" />}
    </div>
  );
  return <section className="source-document-actions" aria-label="原文处理方式">
    {documentAgent ? <details className="document-tools"><summary>文档工具</summary>{actions}</details> : actions}
    {error && <p role="alert">{error}</p>}
    {statusUnavailable && <p role="alert">任务状态暂时无法读取，请重新同步后再发起任务。 <button type="button" className="button ghost compact" onClick={() => setRetry(value => value + 1)}>重新同步任务</button></p>}
    {capture && <details open><summary>快速读取结果 · {capture.page_facts.length ? `${capture.page_facts.length} 页 · ` : ""}{capture.elapsed_seconds.toFixed(2)} 秒{capture.cache_reused ? " · 复用已存原文" : ""}</summary>
      <p>文字、表格和图像已保留。结构是算法候选，尚未判断需求属性；当前原文基线保持不变。</p>
      <p>表格 {capture.capture_counts.table || 0} · 图像 {(capture.capture_counts.image || 0) + (capture.capture_counts.figure || 0)} · 结构候选 {capture.structure_proposal?.nodes.length || 0}</p>
      {capture.gaps.map(gap => <p key={gap.code}>{gap.pages?.length ? `P${gap.pages.join("、")}：` : ""}{gap.message}</p>)}
      <button className="button ghost compact" onClick={downloadCapture}>下载捕获详情</button>
    </details>}
  </section>;
}
