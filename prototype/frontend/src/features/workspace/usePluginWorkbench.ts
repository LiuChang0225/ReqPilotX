import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, request } from "../../shared/api/client";
import type { SourceRequirementRecordDetailV1 } from "../../shared/api/contracts";
import type { SourceWorkbenchReadModel, SourceWorkbenchReader } from "../extraction/SourceWorkbenchReader";
import { retainedCommand, settleCommand } from "../extraction/agentTaskTransport";
import { pluginCapability, type PluginState, type WorkAction } from "./documentWorkProvider";

export type WorkbenchReadClient = { id: string; request: typeof request };

export function usePluginWorkbench({ projectId, documentId, sessionId, sourceSha256, revisionId, onRevision, readClient, readOnly = false, pollIntervalMs = 3000 }: {
  projectId: string; documentId: string; sessionId?: string; sourceSha256: string; revisionId?: string;
  onRevision: (revision: string) => void;
  readClient?: WorkbenchReadClient; readOnly?: boolean; pollIntervalMs?: number;
}) {
  const send = readClient?.request || request;
  const root = `/api/projects/${encodeURIComponent(projectId)}/sor-documents/${encodeURIComponent(documentId)}/agent-structure/delivery/plugin-workbench/${encodeURIComponent(sessionId || "")}`;
  const scope = `${root}:${sourceSha256}${readClient ? `:${readClient.id}` : ""}`, identity = useRef(scope); identity.current = scope;
  const revisionCallback = useRef(onRevision); revisionCallback.current = onRevision;
  const [entry, setEntry] = useState<{ scope: string; value: PluginState } | null>(null);
  const [pin, setPin] = useState<{ scope: string; id: string } | null>(null);
  const [model, setModel] = useState<{ scope: string; value: SourceWorkbenchReadModel } | null>(null);
  const [issue, setIssue] = useState<{ scope: string; text: string } | null>(null);
  const [readIssue, setReadIssue] = useState<{ scope: string; revision: string; text: string } | null>(null);
  const [note, setNote] = useState<{ scope: string; text: string } | null>(null);
  const [pendingScope, setPendingScope] = useState("");
  const [refresh, setRefresh] = useState(0);
  const commands = useRef(new Map<string, { signature: string; id: string }>());
  const state = entry?.scope === scope ? entry.value : null;
  const pinned = revisionId || (pin?.scope === scope ? pin.id : null);
  const current = Boolean(pinned && pinned === state?.revision_id);
  const version = state?.versions.find(v => v.revision_id === pinned);
  const readModel = model?.scope === scope && model.value.revision_id === pinned ? model.value : null;
  const accept = useCallback((next: PluginState) => {
    if (next.source_sha256 !== sourceSha256 || next.session_id !== sessionId || next.project_id && next.project_id !== projectId || next.document_id && next.document_id !== documentId) throw new Error("成果与当前原件或工作会话不一致，请重新选择。");
    if (identity.current !== scope) return;
    setEntry({ scope, value: next });
    if (next.revision_id) setPin(previous => previous?.scope === scope ? previous : { scope, id: next.revision_id! });
  }, [scope, sourceSha256, sessionId, projectId, documentId]);
  useEffect(() => {
    if (!sessionId) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const next = await send<PluginState>(root, { signal: abort.signal });
        if (abort.signal.aborted) return;
        accept(next); setIssue(null);
      } catch (cause) { if (!abort.signal.aborted) setIssue({ scope, text: cause instanceof Error ? cause.message : "暂时无法同步外部成果，已查看的版本保留。" }); }
      if (!abort.signal.aborted && pollIntervalMs > 0) timer = setTimeout(() => void load(), pollIntervalMs);
    };
    void load(); return () => { abort.abort(); clearTimeout(timer); };
  }, [root, scope, sessionId, accept, refresh, send, pollIntervalMs]);
  useEffect(() => { if (sessionId && pinned && !revisionId) revisionCallback.current(pinned); }, [sessionId, pinned, revisionId]);
  useEffect(() => {
    if (!sessionId || !pinned) return;
    if (!/^[a-f0-9]{64}$/.test(pinned)) { setReadIssue({ scope, revision:pinned, text:"版本引用无效，请从已保存版本中重新选择。" }); return; }
    const abort = new AbortController();
    void send<SourceWorkbenchReadModel>(`${root}/reader?revision=${encodeURIComponent(pinned)}`, { signal: abort.signal })
      .then(value => {
        if (abort.signal.aborted) return;
        if (value.revision_id !== pinned || value.source_sha256 !== sourceSha256) throw new Error("成果版本不一致，请重新同步。");
        setModel({ scope, value }); setReadIssue(null);
      }).catch(cause => { if (!abort.signal.aborted) setReadIssue({ scope, revision: pinned, text: cause instanceof Error ? cause.message : "当前核对版暂不可读取。" }); });
    return () => abort.abort();
  }, [root, scope, sessionId, pinned, sourceSha256, refresh, send]);
  const selectRevision = useCallback((id: string) => {
    if (!state?.versions.some(v => v.revision_id === id)) return;
    setPin({ scope, id }); revisionCallback.current(id);
  }, [scope, state]);
  const command = useCallback(async (action: "save_feedback" | "confirm_map", body: Record<string, unknown>) => {
    if (readOnly) throw new Error("当前面板只读，请在关联会话中处理修改。");
    const capability = pluginCapability(state, action, !pinned || current);
    if (!capability.enabled) throw new Error(capability.reason || "此操作当前不可用。");
    if (!pinned || !readModel) throw new Error("请先读取正在核对的版本。");
    const payload = { expected_source_sha256: sourceSha256, expected_revision_id: pinned, ...body };
    const key = `reqpilotx:provider-command:${scope}:${pinned}:${action}`, signature = JSON.stringify(payload);
    const remembered = commands.current.get(key);
    const id = remembered?.signature === signature ? remembered.id : retainedCommand(key, signature);
    commands.current.set(key, { signature, id }); setPendingScope(scope); setNote(null);
    try {
      const next = await send<PluginState>(`${root}/${action === "save_feedback" ? "feedback" : "confirm-map"}`, { method: "POST", body: JSON.stringify({ command_id: id, ...payload }) });
      settleCommand(key, id); commands.current.delete(key);
      accept(next);
      if (identity.current === scope) setRefresh(value => value + 1);
      if (identity.current === scope) setNote({ scope, text: action === "save_feedback" ? "意见已保存，待外部任务取回；未启动新的模型任务。" : "此结构的确认已保存，待外部任务取回后继续。" });
    } catch (cause) {
      settleCommand(key, id, cause);
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) commands.current.delete(key);
      throw cause;
    } finally { if (identity.current === scope) setPendingScope(""); }
  }, [state, current, pinned, readModel, sourceSha256, scope, root, accept, readOnly, send]);
  const saveFeedback = useCallback((text: string, nodeId: string, sourceIds: string[]) => command("save_feedback", { text: text.trim(), node_id: nodeId || null, source_item_ids: sourceIds }), [command]);
  const loadRecord = useCallback((sourceId: string, signal?: AbortSignal) => send<SourceRequirementRecordDetailV1>(`${root}/records/${encodeURIComponent(sourceId)}?revision=${encodeURIComponent(pinned || "")}`, { signal }), [root, pinned, send]);
  const capability = useCallback((action: WorkAction) => readOnly && action !== "export_draft"
    ? { supported: false, enabled: false, reason: "当前面板只读，请在关联会话中处理修改。" }
    : pluginCapability(state, action, !pinned || current), [state, current, pinned, readOnly]);
  const reader = useMemo<SourceWorkbenchReader | undefined>(() => readModel ? { ...readModel, loadRecord, saveFeedback, feedbackCapability: capability("save_feedback") } : undefined, [readModel, loadRecord, saveFeedback, capability]);
  const confirmMap = useCallback(async () => {
    const map = version?.stages.find(stage => stage.stage === "MAP");
    if (!map) throw new Error("这一版尚无可确认的结构。");
    await command("confirm_map", { expected_map_sha256: map.artifact_sha256 });
  }, [version, command]);
  return { scope, state, pinned, current, version, reader, capability, saveFeedback, confirmMap, selectRevision,
    pending: pendingScope === scope, error: readIssue?.scope === scope && readIssue.revision === pinned ? readIssue.text : issue?.scope === scope ? issue.text : "", notice: note?.scope === scope ? note.text : "",
    refresh: () => setRefresh(value => value + 1),
    exportUrl: pinned ? `${root}/export?revision=${encodeURIComponent(pinned)}` : "" };
}
export type PluginWorkbenchProvider = ReturnType<typeof usePluginWorkbench>;
