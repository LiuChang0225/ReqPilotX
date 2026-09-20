import { useEffect, useState } from "react";
import { request } from "../../shared/api/client";
import type { PluginState } from "./documentWorkProvider";
import { readExtractionCheckpoint, readRememberedWorkRevision, type ViewedWork } from "./documentWorkSelection";

export type PluginWorkSummary = Pick<PluginState, "session_id" | "project_id" | "document_id" | "source_sha256" | "revision_id" | "revision_number" | "stages" | "goal" | "execution" | "updated_at"> & {
  title: string; availability: "AVAILABLE" | "NO_ARTIFACTS"; work_ref?: string;
};
export type UnavailableWork = { session_id: string; reason: string; reason_code?: string };
export type DocumentWorkCatalog = {
  schema_version: "reqpilot-plugin-workbench-sessions/v1"; project_id: string; document_id: string; source_sha256: string;
  sessions: PluginWorkSummary[]; unavailable: UnavailableWork[];
};
const validSession = (id: unknown): id is string => typeof id === "string" && /^plugin-[a-f0-9]{32}$/.test(id);
export function validateDocumentWorkCatalog(value: DocumentWorkCatalog, project: string, document: string, source: string): DocumentWorkCatalog {
  if (value?.schema_version !== "reqpilot-plugin-workbench-sessions/v1" || value.project_id !== project || value.document_id !== document || value.source_sha256 !== source || !Array.isArray(value.sessions) || !Array.isArray(value.unavailable)) throw new Error("工作列表与当前文档来源不一致，请重新同步。");
  const unavailable = value.unavailable.filter(item => item && typeof item.session_id === "string").map(item => ({ ...item, reason: typeof item.reason === "string" ? item.reason : "保存的任务暂不可读取。" }));
  const sessions = value.sessions.filter(item => {
    const valid = item && validSession(item.session_id) && item.project_id === project && item.document_id === document && item.source_sha256 === source
      && (item.revision_id === null || typeof item.revision_id === "string" && /^[a-f0-9]{64}$/.test(item.revision_id))
      && Array.isArray(item.stages) && ["AVAILABLE", "NO_ARTIFACTS"].includes(item.availability);
    if (!valid) unavailable.push({ session_id: typeof item?.session_id === "string" ? item.session_id : "unknown", reason: "该任务的来源或成果引用不匹配，未自动切换。" });
    return valid;
  });
  const duplicates = new Set(sessions.filter((item, index) => sessions.findIndex(other => other.session_id === item.session_id) !== index).map(item => item.session_id));
  for (const id of duplicates) unavailable.push({ session_id: id, reason: "工作列表存在重复任务标识，请重新同步。" });
  return { ...value, sessions: sessions.filter(item => !duplicates.has(item.session_id)), unavailable };
}

export function useDocumentWorkCatalog(project: string, document: string, source: string) {
  const scope = `${project}:${document}:${source}`;
  const [entry, setEntry] = useState<{ scope: string; value: DocumentWorkCatalog | null; error: string; loading: boolean } | null>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!project || !document || !source) return;
    const controller = new AbortController();
    setEntry(previous => ({ scope, value: previous?.scope === scope ? previous.value : null, loading: true, error: "" }));
    void request<DocumentWorkCatalog>(`/api/projects/${encodeURIComponent(project)}/sor-documents/${encodeURIComponent(document)}/agent-structure/delivery/plugin-workbench/sessions`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setEntry({ scope, value: validateDocumentWorkCatalog(value, project, document, source), error: "", loading: false }); })
      .catch(cause => { if (!controller.signal.aborted) setEntry(previous => ({ scope, value: previous?.scope === scope ? previous.value : null, error: cause instanceof Error ? cause.message : "工作列表暂不可读取。", loading: false })); });
    return () => controller.abort();
  }, [scope, project, document, source, refresh]);
  const current = entry?.scope === scope ? entry : null;
  return { value: current?.value || null, loading: !current || current.loading, error: current?.error || "", refresh: () => setRefresh(value => value + 1) };
}

export function selectedPluginWork(project: string, document: string, source: string, item: PluginWorkSummary) {
  const work: ViewedWork = { producer: "EXTERNAL_PLUGIN", sessionId: item.session_id,
    revisionId: readRememberedWorkRevision(project, document, source, item.session_id) || item.revision_id || undefined };
  return { work, checkpoint: readExtractionCheckpoint(project, document, source, work) || (item.stages.some(stage => stage.stage === "ITEMS") ? "list" : "structure") };
}
