import { createContext, useContext } from "react";
import type { WorkbenchPresentation } from "../features/workspace/WorkbenchHostContext";

export interface ApplicationWorkflowDescriptor {
  schema: "reqpilot-fixed-workflow/v1"; id: string; version: string; definition_sha256?: string; label?: string;
  stages: { id: string; order: number; label: string; required: boolean }[];
  required_inputs?: { id: string; label: string; required: boolean }[];
  optional_inputs?: { id: string; label: string; available: boolean; reason?: string }[];
  expected_outputs?: string[];
  branches?: Record<string, string>;
}
export interface ApplicationWorkflowStageState {
  id: string; label: string;
  status: "PENDING" | "IN_PROGRESS" | "SAVED" | "SKIPPED" | "WAITING" | "BLOCKED";
  reason?: string; branch?: string;
  evidence?: { kind: string; source_sha256?: string; artifact_sha256?: string; count?: number; path?: string }[];
}
export interface ApplicationWorkflowState {
  schema: "reqpilot-fixed-workflow-state/v1";
  workflow: { id: string; version: string; definition_sha256?: string; brief_sha256?: string; evidence_mode?: string };
  source_sha256: string;
  stages: ApplicationWorkflowStageState[];
  next_stage?: string | null;
  ready_to_review: boolean;
  verification: "SAVED_ARTIFACTS_ONLY" | string;
  open_items: string[];
  human_acceptance: "NOT_CLAIMED" | string;
  knowledge_evidence?: { mode: string; status: string; message?: string; binding_sha256?: string };
}
export interface ApplicationKnowledgeDocument {
  id: string; kbId?: string; name: string; mime?: string; state?: string; pages?: number; expectedPages?: number | null;
  coverage?: "complete" | "partial" | "unknown"; wiki?: string; originalRevision?: string; extractionRevision?: string;
  role?: { authority?: string; claimRole?: string; applicability?: string; label?: string };
  sourceReady?: boolean; job?: { state?: string; message?: string };
}
export interface ApplicationKnowledgeSelection {
  mode: "NONE" | "PROJECT_REFERENCES" | "TEAM_KNOWLEDGE" | "COMBINED";
  projectDocumentIds?: string[];
  team?: { kbId: string; documents?: string[]; topicVersionId?: string | null; authoritative?: boolean } | null;
}
export interface ApplicationKnowledgeInventory {
  available: boolean; reason?: string; applicationId?: string;
  projectLibrary?: { id: string; name: string };
  projectDocuments: ApplicationKnowledgeDocument[];
  teamLibraries: { id: string; name: string; documents: ApplicationKnowledgeDocument[] }[];
}
export interface ApplicationKnowledgeTopic {
  kbId: string; versionId: string; topicId: string; title: string;
  availability: { state: "available" | "draft"; acceptedBy?: string };
  verification: { state: "verified" | "pending"; confirmedBy?: string };
  origin: "ai-organized" | "human-authored";
  applicability: { state: "known" | "unknown"; description?: string };
  authors: string[];
}
export interface ApplicationKnowledgeHost {
  available: boolean;
  inventory(projectId: string): Promise<ApplicationKnowledgeInventory>;
  uploadReference(projectId: string, file: File, kind: "REFERENCE" | "PROJECT_EVIDENCE"): Promise<unknown>;
  retryReference(projectId: string, documentId: string): Promise<unknown>;
  teamTopics(kbId: string): Promise<ApplicationKnowledgeTopic[]>;
  teamLibraries(): Promise<{ id: string; name: string; documents: ApplicationKnowledgeDocument[] }[]>;
}
export interface ApplicationTaskReceipt {
  schema?: string; commandId: string; projectId: string; documentId: string; sourceSha256: string;
  taskId?: string; sessionId: string; workRef: string; status: string;
  modelSelection?: { provider: string; model: string; reasoningEffort?: string };
  selectedAgentModel?: { provider: string; model: string; reasoningEffort?: string };
  workflow?: ApplicationWorkflowDescriptor | { id: string; version: string; schema: string; definition_sha256: string };
  workflowState?: ApplicationWorkflowState | null;
  workflowUnconfirmed?: { reason: string; message?: string } | null;
  controlCapabilities?: { cancel?: boolean; continue?: boolean; reconnect?: boolean };
  knowledgeEvidence?: { mode: string; bindingSha256: string; status: string; message?: string } | null;
  knowledgeStatus?: { state: string; message?: string } | null;
  execution?: { status: string; activity?: string };
}

/** Business execution stays with the embedding host; the shared workbench owns its views. */
export interface WorkbenchApplicationHost extends Pick<WorkbenchPresentation, "resourceUrl" | "pdfViewerUrl"> {
  id: string;
  label: string;
  modelLabel: string;
  captureAvailable: boolean;
  taskStartAvailable: boolean;
  workflow?: ApplicationWorkflowDescriptor | null;
  taskSetup?: string | null;
  knowledge?: ApplicationKnowledgeHost | null;
  startTask(input: { projectId: string; documentId: string; sourceSha256: string; goal: string; requireMapConfirmation?: boolean; knowledge?: ApplicationKnowledgeSelection | null }): Promise<ApplicationTaskReceipt>;
  /** Cancelling an observation discards its pending read without cancelling the durable task. */
  restoreTask(input: { projectId: string; documentId: string; sourceSha256: string; workRef?: string }, options?: { signal?: AbortSignal }): Promise<ApplicationTaskReceipt | null>;
  taskDraft(input: { projectId: string; documentId: string; sourceSha256: string }): { goal: string; requireMapConfirmation: boolean; knowledge?: ApplicationKnowledgeSelection | null } | undefined;
  controlTask(receipt: ApplicationTaskReceipt, action: "cancel" | "continue" | "reconnect"): Promise<ApplicationTaskReceipt>;
  /** View-only by default; explicit context may prepare the selected source without sending or replacing an existing draft. */
  openSession(sessionId: string, context?: { projectId: string; documentId: string; sourceSha256: string; workRef: string; revisionId?: string;
    taskId?: string; scope: "document" | "selected"; nodeId?: string; sourceItemId?: string; derivedUnitId?: string;
    /** Explicit workbench intent carried as draft text; it does not send or claim acceptance. */
    request?: string }): Promise<void>;
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
