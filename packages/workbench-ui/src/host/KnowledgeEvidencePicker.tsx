import { useEffect, useMemo, useState } from "react";
import { useApplicationHost, type ApplicationAdoptedLibrary, type ApplicationKnowledgeInventory, type ApplicationKnowledgeSelection, type ApplicationKnowledgeTopic } from "./ApplicationHost";
import { useUiCopy, type UiTranslate } from "./HostLocaleContext";
import "./knowledge-evidence-picker.css";

const coverageLabel = (l: UiTranslate, value?: string) => value === "partial" ? l("knowledge.coveragePartial", "部分解析") : value === "unknown" ? l("knowledge.coverageUnknown", "解析状态未确认") : l("knowledge.coverageComplete", "已解析");
const wikiLabel = (l: UiTranslate, value?: string) => value === "ready" ? l("knowledge.wikiReady", "Wiki 已生成") : value === "failed" ? l("knowledge.wikiFailed", "Wiki 未完成") : value === "pending" ? l("knowledge.wikiPending", "Wiki 处理中") : l("knowledge.wikiNone", "仅原文");
const originLabel = (l: UiTranslate, value: string) => value === "ai-organized" ? l("knowledge.originAi", "AI 整理") : value === "human-authored" ? l("knowledge.originHuman", "人工编写") : value;

export function KnowledgeEvidencePicker({ projectId, initial, onChange, disabled = false, selectionMode = true }: {
  projectId: string; initial?: ApplicationKnowledgeSelection | null;
  onChange?: (value: ApplicationKnowledgeSelection | null) => void;
  disabled?: boolean; selectionMode?: boolean;
}) {
  const l = useUiCopy();
  const host = useApplicationHost();
  const knowledge = host?.knowledge;
  const [inventory, setInventory] = useState<ApplicationKnowledgeInventory | null>(null);
  const [adoptedLibraries, setAdoptedLibraries] = useState<ApplicationAdoptedLibrary[]>([]);
  const [adoptedChoice, setAdoptedChoice] = useState<{ libraryId: string; versionId: string } | null>(() =>
    initial?.adopted ? { ...initial.adopted } : null);
  const [adoptedLoaded, setAdoptedLoaded] = useState(false);
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
    const adoption = Boolean(knowledge.adoptionAvailable && knowledge.adoptedLibraries);
    if (adoption) setAdoptedLoaded(false);
    try {
      setInventory(await knowledge.inventory(projectId));
      if (adoption && knowledge.adoptedLibraries) setAdoptedLibraries(await knowledge.adoptedLibraries());
    } catch (cause) { setError(cause instanceof Error ? cause.message : l("knowledge.readFailed", "项目依据暂不可读取。")); }
    finally { if (adoption) setAdoptedLoaded(true); setLoading(false); }
  };
  useEffect(() => { void refresh(); }, [knowledge, projectId]);
  const initialKey = JSON.stringify(initial || null);
  useEffect(() => {
    setProjectIds(initial?.projectDocumentIds || []); setTeamId(initial?.team?.kbId || "");
    setTeamMode(initial?.team?.topicVersionId ? "TOPIC" : "LIVE"); setTeamIds(initial?.team?.documents || []);
    setTopicId(initial?.team?.topicVersionId || ""); setAuthoritative(initial?.team?.authoritative === true);
    setAdoptedChoice(initial?.adopted ? { ...initial.adopted } : null);
  }, [projectId, initialKey]);
  useEffect(() => {
    if (!knowledge?.available || !teamId) { setTopics([]); return; }
    const controller = new AbortController();
    void knowledge.teamTopics(teamId).then(value => { if (!controller.signal.aborted) setTopics(value); })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : l("knowledge.topicReadFailed", "保留主题暂不可读取。")); });
    return () => controller.abort();
  }, [knowledge, teamId]);
  const adoptedLibrary = adoptedChoice ? adoptedLibraries.find(item => item.id === adoptedChoice.libraryId) : undefined;
  const adoptedStale = Boolean(adoptedChoice && adoptedLibrary && adoptedLibrary.versionId !== adoptedChoice.versionId);
  const adoptedUnavailable = Boolean(adoptedChoice && (!adoptedLibrary || adoptedLibrary.state === "blocked"));
  const selection = useMemo<ApplicationKnowledgeSelection | null>(() => {
    if (!selectionMode) return null;
    if (adoptedChoice) return { mode: "NONE", adopted: { ...adoptedChoice } };
    const project = projectIds.filter(id => inventory?.projectDocuments.some(item => item.id === id));
    const team = teamId && (teamMode === "LIVE" ? teamIds.length > 0 : Boolean(topicId))
      ? { kbId: teamId, ...(teamMode === "LIVE" ? { documents: teamIds } : { topicVersionId: topicId }), authoritative }
      : null;
    if (!project.length && !team) return null;
    return { mode: project.length && team ? "COMBINED" : project.length ? "PROJECT_REFERENCES" : "TEAM_KNOWLEDGE",
      projectDocumentIds: project, team };
  }, [selectionMode, adoptedChoice, projectIds, teamId, teamMode, teamIds, topicId, authoritative, inventory]);
  useEffect(() => {
    if (adoptedChoice) { onChange?.(selection); return; }
    if (!inventory) return;
    if (knowledge?.adoptionAvailable && !adoptedLoaded) return;
    onChange?.(selection);
  }, [selection, onChange, inventory, adoptedChoice, adoptedLoaded, knowledge?.adoptionAvailable]);
  if (!knowledge?.available) return <p className="knowledge-evidence-unavailable">{l("knowledge.unavailable", "当前宿主未提供 PAIR 知识服务，不能在本次任务中选择内部依据。")}</p>;
  const library = inventory?.teamLibraries.find(item => item.id === teamId);
  const upload = async (files: File[]) => {
    if (!files.length || !projectId) return;
    setBusy("upload"); setError("");
    try {
      for (const file of files) await knowledge.uploadReference(projectId, file, uploadKind);
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : l("knowledge.uploadFailed", "参考资料未导入，请重试。")); }
    finally { setBusy(""); }
  };
  const retry = async (documentId: string) => {
    setBusy("retry:" + documentId); setError("");
    try { await knowledge.retryReference(projectId, documentId); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : l("knowledge.retryFailed", "解析重试未完成。")); }
    finally { setBusy(""); }
  };
  return <section className="knowledge-evidence-picker" aria-label={l("knowledge.pickerAria", "内部依据")}>
    <header><div><strong>{selectionMode ? l("knowledge.pickerTitle", "内部依据（可选）") : l("knowledge.pickerTitleBrowse", "项目与团队依据")}</strong><p>{l("knowledge.pickerNote", "原件、解析与检索由 PAIR 保存；本次选择会冻结到任务版本。")}</p></div><button type="button" className="button ghost compact" disabled={loading || Boolean(busy)} onClick={() => void refresh()}>{loading ? l("knowledge.syncing", "正在同步…") : l("knowledge.syncDirectory", "同步目录")}</button></header>
    {error && <p role="alert" className="knowledge-evidence-error">{error}</p>}
    {knowledge.adoptionAvailable && <section className="knowledge-evidence-adopted">
      <h4>{l("knowledge.adoptedLibraries", "已注册知识库")}</h4>
      <label>{l("knowledge.adoptedSelect", "选择库版本")}<select aria-label={l("knowledge.adoptedSelectAria", "选择已注册知识库")}
        value={adoptedChoice && !adoptedStale && !adoptedUnavailable ? adoptedChoice.libraryId : ""} disabled={disabled}
        onChange={event => {
          const id = event.target.value;
          if (!id) { setAdoptedChoice(null); return; }
          const selected = adoptedLibraries.find(item => item.id === id);
          if (selected && selected.state !== "blocked") setAdoptedChoice({ libraryId: id, versionId: selected.versionId });
        }}>
        <option value="">{adoptedStale ? l("knowledge.adoptedReselect", "请重新选择版本文档") : l("knowledge.adoptedNone", "本次不使用已注册知识库")}</option>
        {adoptedLibraries.map(item => <option key={item.id} value={item.id} disabled={item.state === "blocked"}>
          {item.name} · {l("knowledge.adoptedVersion", "版本 {version}", { version: item.versionId.slice(0, 12) })} · {l(`knowledge.adoptedState.${item.state}`, item.state)}
        </option>)}
      </select></label>
      {!adoptedLibraries.length && <p className="knowledge-evidence-note">{l("knowledge.adoptedEmpty", "当前没有已注册的可选知识库；注册由 PAIR Host 的可信操作完成。")}</p>}
      {adoptedChoice && adoptedStale && <p role="status" className="knowledge-evidence-note">{l("knowledge.adoptedStale", "已恢复冻结版本 {frozen}，当前注册版本已变为 {current}；请重新选择，不会自动切换。", {
        frozen: adoptedChoice.versionId.slice(0, 12), current: adoptedLibrary?.versionId.slice(0, 12) || "—" })}</p>}
      {adoptedChoice && adoptedUnavailable && !adoptedStale && <p role="status" className="knowledge-evidence-note">{l("knowledge.adoptedBlocked", "所选知识库检查未通过或当前不可用，不能用于本次任务。")}</p>}
      {adoptedChoice && !adoptedStale && !adoptedUnavailable && adoptedLibrary && <p className="knowledge-evidence-note">{l("knowledge.adoptedSelected", "冻结版本：{version}{packageVersion}", {
        version: adoptedChoice.versionId.slice(0, 12),
        packageVersion: adoptedLibrary.packageVersion ? ` · ${l("knowledge.adoptedPackage", "包 {version}", { version: adoptedLibrary.packageVersion })}` : "",
      })}{adoptedLibrary.state === "needs-update" ? ` · ${l("knowledge.adoptedNeedsUpdate", "检查建议最小更新；读取仍使用这个冻结版本。")}` : ""}</p>}
      {adoptedChoice && (projectIds.length || teamId) && <p className="knowledge-evidence-note">{l("knowledge.adoptedExclusive", "已注册知识库与项目/团队依据在本次任务中互斥；当前仅使用已注册知识库。")}</p>}
      <p className="knowledge-evidence-note">{l("knowledge.adoptedNote", "只列出 Host 已注册的库；任务会冻结当前版本，不自动切换到新版本。")}</p>
    </section>}
    <section className="knowledge-evidence-project">
      <h4>{l("knowledge.projectReferences", "项目参考资料")} {inventory?.projectLibrary ? <small>· {inventory.projectLibrary.name}</small> : null}</h4>
      {!inventory?.projectDocuments.length && <p className="knowledge-evidence-note">{l("knowledge.noProjectReferences", "当前项目还没有参考资料。上传后仅用于本项目，不自动发布为团队知识。")}</p>}
      {inventory?.projectDocuments.length ? <ul> {inventory.projectDocuments.map(item => <li key={item.id}>
        <label><input type="checkbox" disabled={disabled} checked={projectIds.includes(item.id)}
          onChange={event => setProjectIds(value => event.target.checked ? [...value, item.id] : value.filter(id => id !== item.id))} />
          <span><strong>{item.name}</strong><small>{coverageLabel(l, item.coverage)} · {wikiLabel(l, item.wiki)} · {item.role?.label || item.role?.authority || l("knowledge.roleReference", "参考上下文")}</small></span></label>
        {item.job?.state && !["source-ready", "succeeded"].includes(item.job.state) && <div className="knowledge-evidence-status">
          <span role="status">{item.job.message || item.job.state}{item.coverage === "partial" ? l("knowledge.partiallySearchable", " · 部分可检索") : ""}</span>
          {!["extracting", "waiting_for_worker", "queued", "compiling"].includes(item.job.state) &&
            <button type="button" className="button ghost compact" disabled={disabled || busy === "retry:" + item.id} onClick={() => void retry(item.id)}>{busy === "retry:" + item.id ? l("knowledge.retrying", "正在重试…") : l("knowledge.retryParse", "重试解析")}</button>}
        </div>}
      </li>)}</ul> : null}
      <div className="knowledge-evidence-upload"><label>{l("knowledge.importReference", "导入参考资料")}<input type="file" multiple disabled={disabled || Boolean(busy)} onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ""; void upload(files); }} /></label>
        <select aria-label={l("knowledge.referenceUseAria", "参考资料用途")} value={uploadKind} disabled={disabled || Boolean(busy)} onChange={event => setUploadKind(event.target.value as "REFERENCE" | "PROJECT_EVIDENCE")}>
          <option value="REFERENCE">{l("knowledge.roleReference", "参考上下文")}</option><option value="PROJECT_EVIDENCE">{l("knowledge.declaredProjectEvidence", "已声明项目依据")}</option>
        </select>
        {busy === "upload" && <span role="status">{l("knowledge.importing", "正在导入并解析…")}</span>}
      </div>
      <p className="knowledge-evidence-note">{l("knowledge.referenceNote", "“参考上下文”可帮助理解但不单独证明符合性；“已声明项目依据”仍不替代人工接受。")}</p>
    </section>
    <section className="knowledge-evidence-team">
      <h4>{l("knowledge.teamLibraries", "团队知识库")}</h4>
      {!inventory?.teamLibraries.length ? <p className="knowledge-evidence-note">{l("knowledge.noTeamLibraries", "当前没有可选的团队知识库。")}</p> : <>
        <label>{l("knowledge.selectLibrary", "选择团队库")}<select aria-label={l("knowledge.selectTeamLibraryAria", "选择团队知识库")} value={teamId} disabled={disabled} onChange={event => { setTeamId(event.target.value); setTeamIds([]); setTopicId(""); }}>
          <option value="">{l("knowledge.noTeamKnowledge", "不使用团队知识")}</option>{inventory.teamLibraries.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        {teamId && <><div className="knowledge-evidence-mode" role="group" aria-label={l("knowledge.teamVersionAria", "团队知识版本")}>
          <button type="button" className="button ghost compact" aria-pressed={teamMode === "LIVE"} disabled={disabled} onClick={() => setTeamMode("LIVE")}>{l("knowledge.liveDocuments", "实时文档")}</button>
          <button type="button" className="button ghost compact" aria-pressed={teamMode === "TOPIC"} disabled={disabled} onClick={() => setTeamMode("TOPIC")}>{l("knowledge.savedTopic", "保留主题版本")}</button>
        </div>
        {teamMode === "LIVE" ? <ul>{library?.documents.map(item => <li key={item.id}><label>
          <input type="checkbox" disabled={disabled} checked={teamIds.includes(item.id)} onChange={event => setTeamIds(value => event.target.checked ? [...value, item.id] : value.filter(id => id !== item.id))} />
          <span><strong>{item.name}</strong><small>{coverageLabel(l, item.coverage)} · {wikiLabel(l, item.wiki)}</small></span></label></li>)}</ul>
          : <ul>{topics.map(item => <li key={item.versionId}><label>
            <input type="radio" name="team-topic" disabled={disabled || item.availability.state !== "available"}
              checked={topicId === item.versionId} onChange={() => setTopicId(item.versionId)} />
            <span><strong>{item.title}</strong><small>{[
              item.availability.state === "available" ? l("knowledge.topicAvailable", "可用") : l("knowledge.topicDraft", "草稿（不可选）"),
              item.verification.state === "verified" ? l("knowledge.topicVerified", "已核验") : l("knowledge.topicPendingVerification", "待核验"),
              originLabel(l, item.origin),
              item.availability.acceptedBy,
              item.verification.confirmedBy,
              item.versionId.slice(0, 12),
            ].filter(Boolean).join(" · ")}</small></span></label></li>)}
            {!topics.length && <li className="knowledge-evidence-note">{l("knowledge.noTopics", "该库没有可读取的保留主题版本。")}</li>}</ul>}
        <label className="knowledge-evidence-authority"><input type="checkbox" disabled={disabled} checked={authoritative} onChange={event => setAuthoritative(event.target.checked)} />{l("knowledge.authoritative", "由用户声明为已核准内部依据（仍不等于人工接受）")}</label>
        </>}
      </>}
    </section>
    {selectionMode && <p className="knowledge-evidence-summary" role="status">{l("knowledge.summary", "本次任务依据：{scope}。未选择时不检索全部库。", { scope: selection?.adopted ? l("knowledge.summaryAdopted", "已注册知识库冻结版本") : selection?.mode === "PROJECT_REFERENCES" ? l("knowledge.summaryProject", "仅项目参考") : selection?.mode === "TEAM_KNOWLEDGE" ? l("knowledge.summaryTeam", "仅团队知识") : selection?.mode === "COMBINED" ? l("knowledge.summaryCombined", "项目参考 + 团队知识") : l("knowledge.summaryNone", "未选择内部依据；符合性分析将明确跳过") })}</p>}
  </section>;
}

export function PairKnowledgeWorkspace({ projectId }: { projectId: string }) {
  const l = useUiCopy();
  const host = useApplicationHost();
  return <div className="pair-knowledge-workspace"><header><h1>{l("knowledge.workspaceTitle", "领域知识库")}</h1><p>{projectId ? l("knowledge.workspaceDescription", "项目参考资料与团队知识均通过 PAIR 知识服务读取；每次任务只使用明确选择的子集。") : l("knowledge.workspaceSelectProject", "先从左侧选择项目，再查看项目参考资料与团队知识。")}</p></header>
    {projectId ? <KnowledgeEvidencePicker key={projectId} projectId={projectId} selectionMode={false} /> : null}
    <p className="knowledge-evidence-note">{host?.knowledge?.available ? l("knowledge.workspaceTaskNote", "任务启动后，Agent 只能通过绑定的证据工具读取所选引用；已消费的原文摘录会随分析建议保存。") : l("knowledge.workspaceNoHost", "当前宿主未提供知识服务。")}</p>
  </div>;
}
