import { useCallback, useEffect, useState } from "react";
import { saasStorageKey } from "../../shared/api/saasSession";

type Draft = { schema: 1; text: string; revision: number; updatedAt: string };
const memory = new Map<string, Draft | null>();
const volatileKeys = new Set<string>();
const prefix = "reqpilotx:source-feedback:v1:";
const changedEvent = "reqpilotx:source-feedback-draft";

export function sourceFeedbackDraftKey(project: string, document: string, source: string, baseline: string) {
  return saasStorageKey(prefix + [project, document, source, baseline].map(encodeURIComponent).join(":"));
}

function read(key: string): Draft | null {
  if (memory.has(key)) return memory.get(key) || null;
  try {
    const value = localStorage.getItem(key);
    if (value) {
      const parsed = JSON.parse(value);
      if (parsed.schema === 1 && typeof parsed.text === "string") return parsed;
    }
  } catch { /* Current-window memory remains available when storage is blocked. */ }
  return memory.get(key) || null;
}

function write(key: string, value: Draft | null) {
  memory.set(key, value);
  let durable = true;
  try { if (value) localStorage.setItem(key, JSON.stringify(value)); else localStorage.removeItem(key); }
  catch { durable = false; }
  if (durable) volatileKeys.delete(key); else volatileKeys.add(key);
  window.dispatchEvent(new CustomEvent(changedEvent, { detail: key }));
  return durable;
}

/** Late save responses clear only the exact submitted draft, never newer input. */
export function clearSourceFeedbackDraft(key: string, submitted: string) {
  if ((read(key)?.text || "") !== submitted) return false;
  write(key, null);
  return true;
}

export function useSourceFeedbackDraft(key: string, revision: number) {
  const [state, setState] = useState(() => ({ key, draft: read(key), durable: !volatileKeys.has(key) }));
  // A key change must never render the previous item's draft, even for one frame.
  const current = state.key === key ? state.draft : read(key);
  useEffect(() => {
    setState({ key, draft: read(key), durable: !volatileKeys.has(key) });
    const refresh = (event: Event) => {
      if (event instanceof StorageEvent) {
        if (event.key) memory.delete(event.key); else memory.clear();
      }
      if (!(event instanceof CustomEvent) || event.detail === key) {
        setState({ key, draft: read(key), durable: !volatileKeys.has(key) });
      }
    };
    window.addEventListener(changedEvent, refresh);
    window.addEventListener("storage", refresh);
    return () => { window.removeEventListener(changedEvent, refresh); window.removeEventListener("storage", refresh); };
  }, [key]);
  const update = useCallback((text: string) => {
    const draft: Draft | null = text ? { schema: 1, text, revision, updatedAt: new Date().toISOString() } : null;
    const durable = write(key, draft);
    setState({ key, draft, durable });
  }, [key, revision]);
  const otherDrafts: Array<Draft & { key: string }> = [];
  const scope = key.slice(0, key.lastIndexOf(":") + 1);
  const keys = new Set(memory.keys());
  try { for (let index = 0; index < localStorage.length; index++) { const candidate = localStorage.key(index); if (candidate) keys.add(candidate); } } catch { /* memory only */ }
  for (const candidate of keys) {
    if (candidate === key || !candidate.startsWith(scope)) continue;
    const draft = read(candidate);
    if (draft?.text) otherDrafts.push({ ...draft, key: candidate });
  }
  return { text: current?.text || "", update, durable: state.key === key ? state.durable : !volatileKeys.has(key), otherDrafts };
}
