import { ApiError, request } from "../../shared/api/client";

export type PublicAgentEvent = {
  event_cursor?: number; message_id?: string; version?: number; delta?: string; thread_id?: string; turn_id?: string;
  sequence?: number; type?: string; kind?: string; message?: string;
  tool_name?: string; name?: string; text?: string; phase?: string;
  status?: string; success?: boolean; time_unix?: number; call_id?: string; attempt_id?: string;
};
export async function readAgentEventTail(root: string, taskId: string, after: number, signal: AbortSignal) {
  let cursor = after;
  let tail: PublicAgentEvent[] = [];
  for (let page = 0; page < 8; page += 1) {
    const result = await request<{ events: PublicAgentEvent[]; next_cursor?: number; cursor?: number }>(
      `${root}/${encodeURIComponent(taskId)}/events?after=${cursor}`, { signal });
    if (signal.aborted) return { cursor: after, events: [], hasMore: false };
    const next = result.next_cursor ?? result.cursor ?? cursor;
    tail = [...tail, ...(result.events || [])].slice(-40);
    if (next < cursor) throw new Error("任务记录已更新，请重新同步。");
    if (next === cursor || (result.events?.length || 0) < 100) return { cursor: next, events: tail, hasMore: false };
    cursor = next;
  }
  return { cursor, events: tail, hasMore: true };
}
/** Retry an uncertain HTTP result with the original command and payload. */
export function retainedCommand(storageKey: string, signature: string) {
  try {
    const previous = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (previous?.signature === signature && typeof previous.command_id === "string") return previous.command_id as string;
  } catch { /* The in-memory caller retains a receipt if storage is unavailable. */ }
  const command_id = crypto.randomUUID();
  try { localStorage.setItem(storageKey, JSON.stringify({ signature, command_id })); } catch { /* Optional persistence. */ }
  return command_id;
}
export function settleCommand(storageKey: string, commandId: string, error?: unknown) {
  if (error && !(error instanceof ApiError && error.status >= 400 && error.status < 500)) return;
  try {
    if (JSON.parse(localStorage.getItem(storageKey) || "null")?.command_id === commandId) localStorage.removeItem(storageKey);
  } catch { /* Optional persistence. */ }
}

export type PublicAgentMessage = {
  message_id: string; attempt_id?: string; thread_id?: string; turn_id?: string;
  version: number; text: string; phase: string; status: "streaming" | "complete" | "interrupted";
  time_unix?: number;
};
const publicPhases = new Set(["commentary", "final", "final_answer", "reply"]);
export const messageKey = (message: Pick<PublicAgentMessage, "attempt_id" | "thread_id" | "turn_id" | "message_id">) =>
  JSON.stringify([message.attempt_id, message.thread_id, message.turn_id, message.message_id]);
/** Apply a durable snapshot then ordered events; final item text is authoritative. */
export function reducePublicMessages(previous: PublicAgentMessage[], events: PublicAgentEvent[], snapshot: PublicAgentMessage[] = []) {
  const messages = new Map(previous.map(message => [messageKey(message), message]));
  for (const message of snapshot) {
    if (!publicPhases.has(message.phase) || !message.message_id) continue;
    const key = messageKey(message), old = messages.get(key);
    if (!old || message.version > old.version || message.version === old.version && old.status === "streaming") messages.set(key, { ...message });
  }
  for (const event of events) {
    if (event.type === "turn/completed" || event.type === "runtime_error") {
      for (const [key, message] of messages) if (message.status === "streaming" && (event.type === "runtime_error" || message.turn_id === event.turn_id)) messages.set(key, { ...message, status: "interrupted" });
      continue;
    }
    if (!event.type?.startsWith("agent_message") || !publicPhases.has(event.phase || "reply")) continue;
    const message_id = event.message_id || `legacy:${event.event_cursor ?? event.time_unix}`;
    const key = messageKey({ ...event, message_id });
    const old = messages.get(key), version = event.version ?? 0;
    if (old && version <= old.version) continue;
    const delta = event.type === "agent_message_delta";
    if (delta && (!old || version !== old.version + 1 || old.status !== "streaming")) continue;
    messages.set(key, { message_id, attempt_id: event.attempt_id, thread_id: event.thread_id, turn_id: event.turn_id,
      version, text: delta ? old!.text + (event.delta || "") : event.text || "", phase: event.phase || "reply",
      status: event.type === "agent_message" ? "complete" : "streaming", time_unix: event.time_unix });
  }
  return [...messages.values()].sort((a, b) => (a.time_unix || 0) - (b.time_unix || 0)).slice(-6);
}
