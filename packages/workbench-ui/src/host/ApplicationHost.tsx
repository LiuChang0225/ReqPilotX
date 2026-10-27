import { createContext, useContext } from "react";
import type { WorkbenchPresentation } from "../features/workspace/WorkbenchHostContext";

export interface ApplicationTaskReceipt {
  commandId: string; projectId: string; documentId: string; sourceSha256: string;
  sessionId: string; workRef: string; status: string;
  controlCapabilities?: { cancel?: boolean; continue?: boolean; reconnect?: boolean };
  execution?: { status: string; activity?: string };
}

/** Business execution stays with the embedding host; the shared workbench owns its views. */
export interface WorkbenchApplicationHost extends Pick<WorkbenchPresentation, "resourceUrl" | "pdfViewerUrl"> {
  id: string;
  label: string;
  modelLabel: string;
  captureAvailable: boolean;
  taskStartAvailable: boolean;
  startTask(input: { projectId: string; documentId: string; sourceSha256: string; goal: string; requireMapConfirmation?: boolean }): Promise<ApplicationTaskReceipt>;
  /** Cancelling an observation discards its pending read without cancelling the durable task. */
  restoreTask(input: { projectId: string; documentId: string; sourceSha256: string; workRef?: string }, options?: { signal?: AbortSignal }): Promise<ApplicationTaskReceipt | null>;
  taskDraft(input: { projectId: string; documentId: string; sourceSha256: string }): { goal: string; requireMapConfirmation: boolean } | undefined;
  controlTask(receipt: ApplicationTaskReceipt, action: "cancel" | "continue" | "reconnect"): Promise<ApplicationTaskReceipt>;
  openSession(sessionId: string, context?: { projectId: string; documentId: string; sourceSha256: string; workRef: string; revisionId?: string;
    scope: "document" | "selected"; nodeId?: string; sourceItemId?: string; derivedUnitId?: string }): Promise<void>;
}

export const ApplicationHostContext = createContext<WorkbenchApplicationHost | null>(null);
export const useApplicationHost = () => useContext(ApplicationHostContext);
export const ApplicationWidthContext = createContext<number | undefined>(undefined);
export const useApplicationWidth = () => useContext(ApplicationWidthContext);

/** Host-owned model setup is visible without exposing a second provider configuration. */
export function HostAgentSettings() {
  const host = useApplicationHost();
  if (!host) return null;
  return <section className="settings-panel"><h2>Agent 连接</h2><p>由 {host.label} 提供会话、模型认证和运行环境。</p>
    <dl><dt>任务模型</dt><dd>{host.modelLabel}</dd><dt>登录与连接</dt><dd>请在 {host.label} 的设置 → 模型中核对。入口可见不代表认证已完成。</dd></dl>
    <p>当前工作台继续使用同一份项目、文档、版本和工程师意见。</p></section>;
}
