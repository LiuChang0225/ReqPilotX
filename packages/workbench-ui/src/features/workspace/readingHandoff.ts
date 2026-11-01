import { useEffect, useMemo, useState } from "react";
import { request } from "../../shared/api/client";
import { saasStorageKey } from "../../shared/api/saasSession";
import { sourceFeedbackDraftKey, useSourceFeedbackDraft } from "../extraction/sourceItemDrafts";
import type { ReadingChatMessageV1, ReadingContextV1, ReadingNoteV1 } from "../reading/contracts";
import { readExtractionCheckpoint, readViewedWork, type ViewedWork } from "./documentWorkSelection";
import type { PluginState } from "./documentWorkProvider";
import { serializeWorkspaceRoute } from "./route";

export type ReadingFinding = { kind: "message"; value: ReadingChatMessageV1 } | { kind: "note"; value: ReadingNoteV1 };
export type ReadingHandoffTarget = { projectId: string; documentId: string; sourceSha256: string; work: ViewedWork; url: string };
export function readReadingHandoffTarget(projectId: string, documentId: string, sourceSha256: string): ReadingHandoffTarget | null {
  const work = readViewedWork(projectId, documentId, sourceSha256);
  if (!work) return null;
  return { projectId, documentId, sourceSha256, work, url: serializeWorkspaceRoute({ task: "extraction", mode: "organize", projectId, documentId, requirementSetId: "",
    checkpoint: readExtractionCheckpoint(projectId, documentId, sourceSha256, work) || (work.producer === "EXTERNAL_PLUGIN" && !work.revisionId ? "structure" : "list"),
    ...(work.producer === "EXTERNAL_PLUGIN" ? { pluginSessionId: work.sessionId, resultRevision: work.revisionId } : {}) }) };
}

export function validateReadingHandoffTarget(target: ReadingHandoffTarget, state: PluginState) {
  if (state.source_sha256 !== target.sourceSha256 || state.session_id !== target.work.sessionId || state.project_id && state.project_id !== target.projectId || state.document_id && state.document_id !== target.documentId) throw new Error("工作成果与当前阅读来源不一致，未准备草稿。");
  if (!target.work.revisionId) throw new Error("当前任务尚无阶段成果；形成成果后再准备工作台草稿。");
  const version = state.versions.find(item => item.revision_id === target.work.revisionId);
  if (!version) throw new Error("原先查看的成果版本已不可读取；不会改绑最新版本。");
  return { number: version.number, historical: state.revision_id !== version.revision_id };
}

export function buildReadingFindingDraft(context: ReadingContextV1, finding: ReadingFinding, target: ReadingHandoffTarget) {
  const value = finding.value;
  if (target.sourceSha256 !== context.source_sha256 || target.projectId !== context.project_id || target.documentId !== context.sor_document_id || value.project_id !== context.project_id || value.sor_document_id !== context.sor_document_id || value.source_sha256 !== context.source_sha256 || value.reading_session_id !== context.reading_session_id || value.status !== "ACTIVE") throw new Error("仅能带回当前来源的有效阅读结果；旧捕获内容保留在阅读历史中。");
  if (finding.kind === "message" && (finding.value.role !== "ASSISTANT" || finding.value.effective_bundle_id !== context.effective_bundle_id)) throw new Error("回答未绑定当前捕获版本，不能准备工作台草稿。");
  const provenance = { project_id: context.project_id, document_id: context.sor_document_id, source_sha256: context.source_sha256,
    reading_session_id: context.reading_session_id, effective_bundle_id: context.effective_bundle_id,
    target_producer: target.work.producer, target_session_id: target.work.sessionId, target_revision_id: target.work.revisionId,
    ...(finding.kind === "message" ? { conversation_id: finding.value.conversation_id, message_id: finding.value.message_id,
      citations: finding.value.citations, source_refs: finding.value.source_refs || (finding.value.source_ref ? [finding.value.source_ref] : []) }
      : { note_id: finding.value.note_id, note_revision: finding.value.revision, source_ref: finding.value.source_ref }) };
  const text = `待核对的阅读发现（尚未提交）\n以下是阅读回答或个人笔记，不是客户新增需求、内部符合性证据或工程师结论。请结合所附原文定位核对，再决定是否修订。\n\n${value.content}\n\n来源与接续记录：\n${JSON.stringify(provenance, null, 2)}`;
  if (text.length > 12000) throw new Error("完整内容和来源记录超过工作台草稿上限；未截断，请先保存一条范围明确的阅读笔记。");
  return text;
}

export function useReadingHandoff(context: ReadingContextV1 | null) {
  const scope = context ? `${context.project_id}:${context.sor_document_id}:${context.source_sha256}` : "";
  const target = useMemo(() => context ? readReadingHandoffTarget(context.project_id, context.sor_document_id, context.source_sha256) : null, [scope]);
  const [check, setCheck] = useState<{ scope: string; number?: number; historical?: boolean; error?: string } | null>(null);
  const [retry, setRetry] = useState(0);
  const draftKey = target?.work.producer === "EXTERNAL_PLUGIN" ? sourceFeedbackDraftKey(target.projectId, target.documentId, `external:${target.work.sessionId}:document`, target.work.revisionId!) : "reqpilotx:reading-handoff:unavailable";
  const draft = useSourceFeedbackDraft(draftKey, check?.scope === scope ? check.number || 0 : 0);
  useEffect(() => {
    if (!target || target.work.producer !== "EXTERNAL_PLUGIN") return;
    const controller = new AbortController(); setCheck(null);
    const root = `/api/projects/${encodeURIComponent(target.projectId)}/sor-documents/${encodeURIComponent(target.documentId)}/agent-structure/delivery/plugin-workbench/${encodeURIComponent(target.work.sessionId!)}`;
    void request<PluginState>(root, { signal: controller.signal }).then(state => {
      if (!controller.signal.aborted) setCheck({ scope, ...validateReadingHandoffTarget(target, state) });
    }).catch(cause => { if (!controller.signal.aborted) setCheck({ scope, error: cause instanceof Error ? cause.message : "暂时无法核对工作版本。" }); });
    return () => controller.abort();
  }, [target, scope, retry]);
  const checked = check?.scope === scope ? check : null;
  const reason = !context ? "正在准备阅读来源。" : !target ? "尚未找到与当前来源绑定的工作记录；可先返回项目打开工作台。" : target.work.producer === "NATIVE" ? "可返回原生工作；尚未核对任务与基线身份，本入口暂不写入原生任务草稿。" : checked?.error || (!checked?.number ? "正在核对原先查看的插件成果版本…" : "");
  const prepare = (finding: ReadingFinding) => {
    if (reason || !target || !context) throw new Error(reason || "当前工作不可用。");
    const text = buildReadingFindingDraft(context, finding, target);
    let stored: { text?: string } | null;
    try {
      const raw = localStorage.getItem(draftKey); stored = raw ? JSON.parse(raw) : null;
      if (raw && (!stored || (stored as { schema?: number }).schema !== 1 || typeof stored.text !== "string")) throw new Error("Unknown draft format");
    }
    catch { throw new Error("无法确认已有本机草稿，未写入；阅读内容仍保留。"); }
    if (draft.text || stored?.text) throw new Error("该版本的整个文档已有未发送草稿，未覆盖。请返回当前工作处理已有草稿后再带回。");
    draft.update(text);
    return text;
  };
  return { target, reason, number: checked?.number, historical: checked?.historical, canRetry: Boolean(checked?.error), refresh: () => setRetry(value => value + 1), prepare, durable: draft.durable };
}
export type ReadingHandoff = ReturnType<typeof useReadingHandoff>;

const intentKey = "reqpilotx:reading-handoff-intent:v1";
type HandoffIdentity = { projectId: string; documentId: string; sourceSha256: string; sessionId?: string; revisionId?: string };
export function storeReadingHandoffIntent(target: ReadingHandoffTarget) {
  if (target.work.producer !== "EXTERNAL_PLUGIN") throw new Error("当前工作尚不支持此草稿交接。");
  const intent = { projectId: target.projectId, documentId: target.documentId, sourceSha256: target.sourceSha256,
    sessionId: target.work.sessionId, revisionId: target.work.revisionId, scope: "document" };
  try { sessionStorage.setItem(saasStorageKey(intentKey), JSON.stringify(intent)); }
  catch { throw new Error("草稿已准备，但暂时无法打开交接入口；草稿没有发送，请重试打开。"); }
}
export function consumeReadingHandoffIntent(identity: HandoffIdentity): { scope: "document"; draftKey: string } | null {
  if (!identity.sessionId || !identity.revisionId || !identity.sourceSha256) return null;
  try {
    const intent = JSON.parse(sessionStorage.getItem(saasStorageKey(intentKey)) || "null");
    if (!intent || intent.scope !== "document" || (["projectId", "documentId", "sourceSha256", "sessionId", "revisionId"] as const).some(key => intent[key] !== identity[key])) return null;
    sessionStorage.removeItem(saasStorageKey(intentKey));
    return { scope: "document", draftKey: sourceFeedbackDraftKey(identity.projectId, identity.documentId, `external:${identity.sessionId}:document`, identity.revisionId) };
  } catch { return null; }
}
