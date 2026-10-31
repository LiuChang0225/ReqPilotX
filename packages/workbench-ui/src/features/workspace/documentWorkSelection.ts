import { saasStorageKey } from "../../shared/api/saasSession";

export type ViewedWork = { producer: "NATIVE" | "EXTERNAL_PLUGIN"; sessionId?: string; revisionId?: string };
const key = (project: string, document: string, source: string) => saasStorageKey(`reqpilotx:viewed-work:v1:${project}:${document}:${source}`);
const revisionKey = (project: string, document: string, source: string, session: string) => `${key(project, document, source)}:session:${session}`;
export function rememberViewedWork(project: string, document: string, source: string, value: ViewedWork) {
  if (!project || !document || !source) return;
  try {
    localStorage.setItem(key(project,document,source),JSON.stringify(value));
    if (value.producer === "EXTERNAL_PLUGIN" && value.sessionId && value.revisionId) localStorage.setItem(revisionKey(project, document, source, value.sessionId), value.revisionId);
  } catch { /* URL still preserves the viewed version. */ }
}
export function readViewedWork(project: string, document: string, source: string): ViewedWork | null {
  if (!project || !document || !source) return null;
  try {
    const value=JSON.parse(localStorage.getItem(key(project,document,source)) || "null");
    if(value?.producer === "NATIVE") return {producer:"NATIVE"};
    if(value?.producer === "EXTERNAL_PLUGIN" && /^plugin-[a-f0-9]{32}$/.test(value.sessionId) && (value.revisionId == null || /^[a-f0-9]{64}$/.test(value.revisionId))) return { producer: "EXTERNAL_PLUGIN", sessionId: value.sessionId, revisionId: value.revisionId || undefined };
  } catch { /* Missing or invalid preference never changes business identity. */ }
  return null;
}

export function readRememberedWorkRevision(project: string, document: string, source: string, session: string): string | undefined {
  if (!project || !document || !source || !session) return;
  try { const value = localStorage.getItem(revisionKey(project, document, source, session)); if (value && /^[a-f0-9]{64}$/.test(value)) return value; } catch { /* An explicit selection can still open the available head. */ }
}


export type OrganizeCheckpoint = "diagnose" | "structure" | "list";
const checkpointKey = (project: string, document: string, source: string, work: ViewedWork) =>
  saasStorageKey(`reqpilotx:extraction-checkpoint:v1:${project}:${document}:${source}:${work.producer}:${work.sessionId || ""}:${work.revisionId || ""}`);

/** A viewing preference is isolated from both the business source and the selected result version. */
export function rememberExtractionCheckpoint(project: string, document: string, source: string, work: ViewedWork, checkpoint: OrganizeCheckpoint) {
  if (!project || !document || !source) return;
  try { localStorage.setItem(checkpointKey(project, document, source, work), checkpoint); } catch { /* Navigation works without optional local preferences. */ }
}

export function readExtractionCheckpoint(project: string, document: string, source: string, work: ViewedWork): OrganizeCheckpoint | undefined {
  if (!project || !document || !source) return undefined;
  try {
    const value = localStorage.getItem(checkpointKey(project, document, source, work));
    if (value === "diagnose" || value === "structure" || value === "list") return value;
  } catch { /* A missing preference must not select a different source. */ }
  return undefined;
}
