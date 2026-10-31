import { useEffect, useMemo, useState } from "react";
import { useApplicationHost, type ApplicationKnowledgeInventory, type ApplicationKnowledgeSelection, type ApplicationKnowledgeTopic } from "./ApplicationHost";
import "./knowledge-evidence-picker.css";

const coverageLabel = (value?: string) => value === "partial" ? "部分解析" : value === "unknown" ? "解析状态未确认" : "已解析";
const wikiLabel = (value?: string) => value === "ready" ? "Wiki 已生成" : value === "failed" ? "Wiki 未完成" : value === "pending" ? "Wiki 处理中" : "仅原文";

export function KnowledgeEvidencePicker({ projectId, initial, onChange, disabled = false, selectionMode = true }: {
  projectId: string; initial?: ApplicationKnowledgeSelection | null;
  onChange?: (value: ApplicationKnowledgeSelection | null) => void;
  disabled?: boolean; selectionMode?: boolean;
}) {
  const host = useApplicationHost();
  const knowledge = host?.knowledge;
  const [inventory, setInventory] = useState<ApplicationKnowledgeInventory | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [projectIds, setProjectIds] = useState<string[]>(() => initial?.projectDocumentIds || []);
  const [teamId, setTeamId] = useState(() => initial?.team?.kbId || "");
  const [teamMode, setTeamMode] = useState<"LIVE" | "TOPIC">(() => initial?.team?.topicVersionId ? "TOPIC" : "LIVE");
  const [teamIds, setTeamIds] = useState<string[]>(() => initial?.team?.documents || []);
  const [topicId, setTopicId] = useState(() => initial?.team?.topicVersionId || "");
  const [topics, setTopics] = useState<ApplicationKnowledgeTopic[]>([]);
  const [authoritative, setAuthoritative] = useState(() => initial?.team?.authoritative === true);
  const [uploadKind, setUploadKind] = useState<"REFERENCE" | "PROJECT_EVIDENCE">("REFERENCE");
  const [busy, setBusy] = useState("");

  const refresh = async () => {
    if (!knowledge?.available || !projectId) return;
    setLoading(true); setError("");
    try { setInventory(await knowledge.inventory(projectId)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "项目依据暂不可读取。"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, [knowledge, projectId]);
  const initialKey = JSON.stringify(initial || null);
  useEffect(() => {
    setProjectIds(initial?.projectDocumentIds || []); setTeamId(initial?.team?.kbId || "");
    setTeamMode(initial?.team?.topicVersionId ? "TOPIC" : "LIVE"); setTeamIds(initial?.team?.documents || []);
    setTopicId(initial?.team?.topicVersionId || ""); setAuthoritative(initial?.team?.authoritative === true);
  }, [projectId, initialKey]);
  useEffect(() => {
    if (!knowledge?.available || !teamId) { setTopics([]); return; }
    const controller = new AbortController();
    void knowledge.teamTopics(teamId).then(value => { if (!controller.signal.aborted) setTopics(value); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "保留主题暂不可读取。"); });
    return () => controller.abort();
  }, [knowledge, teamId]);
  const selection = useMemo<ApplicationKnowledgeSelection | null>(() => {
    if (!selectionMode) return null;
    const project = projectIds.filter(id => inventory?.projectDocuments.some(item => item.id === id));
    const team = teamId && (teamMode === "LIVE" ? teamIds.length > 0 : Boolean(topicId))
      ? { kbId: teamId, ...(teamMode === "LIVE" ? { documents: teamIds } : { topicVersionId: topicId }), authoritative }
      : null;
    if (!project.length && !team) return null;
    return { mode: project.length && team ? "COMBINED" : project.length ? "PROJECT_REFERENCES" : "TEAM_KNOWLEDGE",
      projectDocumentIds: project, team };
  }, [selectionMode, projectIds, teamId, teamMode, teamIds, topicId, authoritative, inventory]);
  useEffect(() => { if (inventory) onChange?.(selection); }, [selection, onChange, inventory]);
  if (!knowledge?.available) return <p className="knowledge-evidence-unavailable">当前宿主未提供 PAIR 知识服务，不能在本次任务中选择内部依据。</p>;
  const library = inventory?.teamLibraries.find(item => item.id === teamId);
  const upload = async (files: File[]) => {
    if (!files.length || !projectId) return;
    setBusy("upload"); setError("");
    try {
      for (const file of files) await knowledge.uploadReference(projectId, file, uploadKind);
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "参考资料未导入，请重试。"); }
    finally { setBusy(""); }
  };
  const retry = async (documentId: string) => {
    setBusy("retry:" + documentId); setError("");
    try { await knowledge.retryReference(projectId, documentId); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "解析重试未完成。"); }
    finally { setBusy(""); }
  };
  return <section className="knowledge-evidence-picker" aria-label="内部依据">
    <header><div><strong>{selectionMode ? "内部依据（可选）" : "项目与团队依据"}</strong><p>原件、解析与检索由 PAIR 保存；本次选择会冻结到任务版本。</p></div><button type="button" className="button ghost compact" disabled={loading || Boolean(busy)} onClick={() => void refresh()}>{loading ? "正在同步…" : "同步目录"}</button></header>
    {error && <p role="alert" className="knowledge-evidence-error">{error}</p>}
    <section className="knowledge-evidence-project">
      <h4>项目参考资料 {inventory?.projectLibrary ? <small>· {inventory.projectLibrary.name}</small> : null}</h4>
      {!inventory?.projectDocuments.length && <p className="knowledge-evidence-note">当前项目还没有参考资料。上传后仅用于本项目，不自动发布为团队知识。</p>}
      {inventory?.projectDocuments.length ? <ul> {inventory.projectDocuments.map(item => <li key={item.id}>
        <label><input type="checkbox" disabled={disabled} checked={projectIds.includes(item.id)}
          onChange={event => setProjectIds(value => event.target.checked ? [...value, item.id] : value.filter(id => id !== item.id))} />
          <span><strong>{item.name}</strong><small>{coverageLabel(item.coverage)} · {wikiLabel(item.wiki)} · {item.role?.label || item.role?.authority || "参考上下文"}</small></span></label>
        {item.job?.state && !["source-ready", "succeeded"].includes(item.job.state) && <div className="knowledge-evidence-status">
          <span role="status">{item.job.message || item.job.state}{item.coverage === "partial" ? " · 部分可检索" : ""}</span>
          {!["extracting", "waiting_for_worker", "queued", "compiling"].includes(item.job.state) &&
            <button type="button" className="button ghost compact" disabled={disabled || busy === "retry:" + item.id} onClick={() => void retry(item.id)}>{busy === "retry:" + item.id ? "正在重试…" : "重试解析"}</button>}
        </div>}
      </li>)}</ul> : null}
      <div className="knowledge-evidence-upload"><label>导入参考资料<input type="file" multiple disabled={disabled || Boolean(busy)} onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ""; void upload(files); }} /></label>
        <select aria-label="参考资料用途" value={uploadKind} disabled={disabled || Boolean(busy)} onChange={event => setUploadKind(event.target.value as "REFERENCE" | "PROJECT_EVIDENCE")}>
          <option value="REFERENCE">参考上下文</option><option value="PROJECT_EVIDENCE">已声明项目依据</option>
        </select>
        {busy === "upload" && <span role="status">正在导入并解析…</span>}
      </div>
      <p className="knowledge-evidence-note">“参考上下文”可帮助理解但不单独证明符合性；“已声明项目依据”仍不替代人工接受。</p>
    </section>
    <section className="knowledge-evidence-team">
      <h4>团队知识库</h4>
      {!inventory?.teamLibraries.length ? <p className="knowledge-evidence-note">当前没有可选的团队知识库。</p> : <>
        <label>选择团队库<select aria-label="选择团队知识库" value={teamId} disabled={disabled} onChange={event => { setTeamId(event.target.value); setTeamIds([]); setTopicId(""); }}>
          <option value="">不使用团队知识</option>{inventory.teamLibraries.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        {teamId && <><div className="knowledge-evidence-mode" role="group" aria-label="团队知识版本">
          <button type="button" className="button ghost compact" aria-pressed={teamMode === "LIVE"} disabled={disabled} onClick={() => setTeamMode("LIVE")}>实时文档</button>
          <button type="button" className="button ghost compact" aria-pressed={teamMode === "TOPIC"} disabled={disabled} onClick={() => setTeamMode("TOPIC")}>保留主题版本</button>
        </div>
        {teamMode === "LIVE" ? <ul>{library?.documents.map(item => <li key={item.id}><label>
          <input type="checkbox" disabled={disabled} checked={teamIds.includes(item.id)} onChange={event => setTeamIds(value => event.target.checked ? [...value, item.id] : value.filter(id => id !== item.id))} />
          <span><strong>{item.name}</strong><small>{coverageLabel(item.coverage)} · {wikiLabel(item.wiki)}</small></span></label></li>)}</ul>
          : <ul>{topics.map(item => <li key={item.versionId}><label>
            <input type="radio" name="team-topic" disabled={disabled || item.availability.state !== "available"}
              checked={topicId === item.versionId} onChange={() => setTopicId(item.versionId)} />
            <span><strong>{item.title}</strong><small>{[
              item.availability.state === "available" ? "可用" : "草稿（不可选）",
              item.verification.state === "verified" ? "已核验" : "待核验",
              item.origin,
              item.availability.acceptedBy,
              item.verification.confirmedBy,
              item.versionId.slice(0, 12),
            ].filter(Boolean).join(" · ")}</small></span></label></li>)}
            {!topics.length && <li className="knowledge-evidence-note">该库没有可读取的保留主题版本。</li>}</ul>}
        <label className="knowledge-evidence-authority"><input type="checkbox" disabled={disabled} checked={authoritative} onChange={event => setAuthoritative(event.target.checked)} />由用户声明为已核准内部依据（仍不等于人工接受）</label>
        </>}
      </>}
    </section>
    {selectionMode && <p className="knowledge-evidence-summary" role="status">本次任务依据：{selection?.mode === "PROJECT_REFERENCES" ? "仅项目参考" : selection?.mode === "TEAM_KNOWLEDGE" ? "仅团队知识" : selection?.mode === "COMBINED" ? "项目参考 + 团队知识" : "未选择内部依据；符合性分析将明确跳过"}。未选择时不检索全部库。</p>}
  </section>;
}

export function PairKnowledgeWorkspace({ projectId }: { projectId: string }) {
  const host = useApplicationHost();
  return <div className="pair-knowledge-workspace"><header><h1>领域知识库</h1><p>{projectId ? "项目参考资料与团队知识均通过 PAIR 知识服务读取；每次任务只使用明确选择的子集。" : "先从左侧选择项目，再查看项目参考资料与团队知识。"}</p></header>
    {projectId ? <KnowledgeEvidencePicker key={projectId} projectId={projectId} selectionMode={false} /> : null}
    <p className="knowledge-evidence-note">{host?.knowledge?.available ? "任务启动后，Agent 只能通过绑定的证据工具读取所选引用；已消费的原文摘录会随分析建议保存。" : "当前宿主未提供知识服务。"}</p>
  </div>;
}
