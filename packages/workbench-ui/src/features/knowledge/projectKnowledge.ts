import { useCallback, useEffect, useState } from "react";
import { request } from "../../shared/api/client";

export type ProjectKnowledgeDocument = { id: string; project_id: string; file_name: string; source_sha256: string; doc_version?: string; status?: string;
  authority?: string; claim_role?: string; applicability?: string; eligible: boolean; eligibility_reason?: string };
export type ProjectKnowledgeFact = { fact_id: string; document_id: string; document_name?: string; source_sha256: string; text: string;
  source_ref?: { location?: { page_number?: number }; format?: string; source_node_id?: string }; table?: unknown };
export type ProjectKnowledgeCatalog = { schema_version: "project-knowledge-facts/v1"; project_id: string; knowledge_base_version: number;
  documents: ProjectKnowledgeDocument[]; evidence_catalog: Record<string, ProjectKnowledgeFact>; gaps: Array<{ document_id?: string; code: string; message?: string }> };

export function validateProjectKnowledgeCatalog(value: ProjectKnowledgeCatalog, project: string) {
  if (value?.schema_version !== "project-knowledge-facts/v1" || value.project_id !== project || !Array.isArray(value.documents) || !value.evidence_catalog || typeof value.evidence_catalog !== "object" || Array.isArray(value.evidence_catalog)) throw new Error("资料目录与当前项目不一致，请重新同步。");
  if (value.documents.some(item => !item || item.project_id !== project || !item.id || typeof item.file_name !== "string" || typeof item.eligible !== "boolean" || !/^[a-f0-9]{64}$/.test(item.source_sha256))) throw new Error("资料来源身份不完整，未显示其他项目内容。");
  const documents = new Map(value.documents.map(item => [item.id, item]));
  if (documents.size !== value.documents.length) throw new Error("资料目录包含重复身份，请重新同步。");
  if (Object.entries(value.evidence_catalog).some(([id, fact]) => !fact || fact.fact_id !== id || typeof fact.text !== "string" || !documents.has(fact.document_id) || documents.get(fact.document_id)?.source_sha256 !== fact.source_sha256)) throw new Error("引用内容与登记原件版本不一致，请重新同步。");
  return value;
}

export function useProjectKnowledgeCatalog(project: string) {
  const [entry, setEntry] = useState<{ project: string; value: ProjectKnowledgeCatalog | null; error: string; loading: boolean } | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!project) return;
    const controller = new AbortController();
    setEntry(previous => ({ project, value: previous?.project === project ? previous.value : null, error: "", loading: true }));
    void request<ProjectKnowledgeCatalog>(`/api/projects/${encodeURIComponent(project)}/knowledge-facts`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setEntry({ project, value: validateProjectKnowledgeCatalog(value, project), error: "", loading: false }); })
      .catch(cause => { if (!controller.signal.aborted) setEntry(previous => ({ project, value: previous?.project === project ? previous.value : null, loading: false, error: cause instanceof Error ? cause.message : "项目资料暂不可读取。" })); });
    return () => controller.abort();
  }, [project, revision]);
  const current = entry?.project === project ? entry : null;
  return { catalog: current?.value || null, error: current?.error || "", loading: Boolean(project) && (!current || current.loading), refresh: () => setRevision(value => value + 1) };
}

export function projectKnowledgeFileUrl(project: string, document: string, sourceSha: string) {
  if (!project || !document || !/^[a-f0-9]{64}$/.test(sourceSha)) return "";
  return `/api/projects/${encodeURIComponent(project)}/knowledge-documents/${encodeURIComponent(document)}/file?expected_source_sha256=${encodeURIComponent(sourceSha)}`;
}

function readSelection(key: string): string[] | null {
  try { const value = JSON.parse(localStorage.getItem(key) || "null"); return Array.isArray(value) && value.every(id => typeof id === "string") ? value : null; } catch { return null; }
}
export function useAnalysisKnowledgeSelection(scope: string) {
  const key = `reqpilotx:analysis-knowledge-selection:v1:${scope}`;
  const [entry, setEntry] = useState(() => ({ key, ids: readSelection(key), durable: true }));
  const ids = entry.key === key ? entry.ids : readSelection(key);
  const setIds = useCallback((next: string[] | null) => {
    const ids = next === null ? null : [...new Set(next)];
    let durable = true;
    try { if (ids === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(ids)); } catch { durable = false; }
    setEntry({ key, ids, durable });
  }, [key]);
  return { ids, setIds, durable: entry.key !== key || entry.durable };
}
