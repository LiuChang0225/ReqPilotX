import { requestWithTransport } from "../shared/api/client";
import type { WorkbenchReadClient } from "../features/workspace/usePluginWorkbench";
import type { WorkbenchPresentation } from "../features/workspace/WorkbenchHostContext";

export type ReadonlyWorkSelection = {
  projectId: string; documentId: string; sourceSha256: string; workRef: string; revisionId?: string;
  view?: "structure" | "items" | "analysis" | "export";
};
export type SessionBinding = Pick<ReadonlyWorkSelection, "projectId" | "documentId" | "sourceSha256" | "workRef"> & {
  hostId: string; workspaceId: string; sessionId: string;
};
export type SessionDiscussionContext = ReadonlyWorkSelection & {
  scope: "document" | "selected"; nodeId?: string; sourceItemId?: string; derivedUnitId?: string;
};
export type HostExecutionSnapshot = SessionBinding & {
  runId: string; status: "RUNNING" | "WAITING" | "IDLE" | "SUCCEEDED" | "FAILED" | "INTERRUPTED";
  observedAt: string; connected: boolean; activityLabel?: string;
};
export type ExecutionSource = {
  /** Return a stable snapshot object until a real host lifecycle event changes it. */
  getSnapshot: () => HostExecutionSnapshot | null;
  subscribe: (listener: () => void) => () => void;
};
export type WorkUpdatedEvent = Pick<ReadonlyWorkSelection, "projectId" | "documentId" | "sourceSha256" | "workRef"> & { revisionId: string };
export type ReadonlyWorkbenchHost = {
  id: string; label: string; client: WorkbenchReadClient;
  resourceUrl: WorkbenchPresentation["resourceUrl"];
  pdfViewerUrl: WorkbenchPresentation["pdfViewerUrl"];
  sessionBinding?: SessionBinding | null;
  execution?: ExecutionSource;
  /** Only a user action calls this. The adapter opens the bound conversation and prepares context; it must not auto-send a prompt. */
  openSession?: (binding: SessionBinding, context: SessionDiscussionContext) => void | Promise<void>;
  subscribeWorkUpdates?: (listener: (event: WorkUpdatedEvent) => void) => () => void;
  onSelectionChange?: (selection: ReadonlyWorkSelection) => void;
};

export function sameWork(left: Pick<ReadonlyWorkSelection, "projectId" | "documentId" | "sourceSha256" | "workRef">, right: Pick<ReadonlyWorkSelection, "projectId" | "documentId" | "sourceSha256" | "workRef">) {
  return left.projectId === right.projectId && left.documentId === right.documentId && left.sourceSha256 === right.sourceSha256 && left.workRef === right.workRef;
}
export function validWorkSelection(value: ReadonlyWorkSelection) {
  return Boolean(value.projectId && value.documentId && /^[a-f0-9]{64}$/.test(value.sourceSha256)
    && /^plugin-[a-f0-9]{32}$/.test(value.workRef) && (!value.revisionId || /^[a-f0-9]{64}$/.test(value.revisionId)));
}
export function matchingSession(selection: ReadonlyWorkSelection, binding?: SessionBinding | null) {
  return binding && binding.hostId && binding.workspaceId && binding.sessionId && sameWork(selection, binding) ? binding : null;
}
export function matchingExecution(binding: SessionBinding | null, snapshot: HostExecutionSnapshot | null) {
  return binding && snapshot && sameWork(binding, snapshot) && binding.hostId === snapshot.hostId
    && binding.workspaceId === snapshot.workspaceId && binding.sessionId === snapshot.sessionId
    && snapshot.runId && typeof snapshot.connected === "boolean" && ["RUNNING", "WAITING", "IDLE", "SUCCEEDED", "FAILED", "INTERRUPTED"].includes(snapshot.status)
    && Number.isFinite(Date.parse(snapshot.observedAt)) ? snapshot : null;
}

/** Convenience for a same-origin proxy or an already authorized HTTP transport. No global fetch patching. */
export function createReadonlyHttpClient(id: string, resolveUrl: (path: string) => string, transport: typeof fetch = (input, init) => fetch(input, init)): WorkbenchReadClient {
  return { id, request: (path, init, options) => {
    if (init?.method && !["GET", "HEAD"].includes(init.method.toUpperCase())) return Promise.reject(new Error("只读连接不允许写入。"));
    return requestWithTransport(transport, resolveUrl(path), init, options);
  } };
}
