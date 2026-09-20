import { createContext, type PropsWithChildren, useContext } from "react";
import type {
  AiProviderSetup,
  ExtractionStatus,
  JsonRecord,
  KnowledgeDocument,
  ProjectProcessingProfile,
  Project,
  RequirementSetsPayload,
  RuntimeReadiness,
  RuntimeCapability,
  SorDocument,
  StructureOptimizationPayload,
  StructurePayload,
  WorkspacePayload,
} from "../../shared/api/contracts";
import type { WorkspaceRoute } from "./route";

export function projectScopeKey(projectId: string): string {
  return projectId.trim();
}

export function documentScopeKey(
  projectId: string,
  documentId: string,
): string {
  const projectScope = projectScopeKey(projectId);
  const documentScope = documentId.trim();
  return projectScope && documentScope
    ? `${projectScope}::${documentScope}`
    : "";
}

export function requirementSetScopeKey(
  projectId: string,
  documentId: string,
  requirementSetId: string,
): string {
  const documentScope = documentScopeKey(projectId, documentId);
  return documentScope ? `${documentScope}::${requirementSetId.trim()}` : "";
}

export function exactScopeValue<T>(
  value: T,
  valueScope: string,
  currentScope: string,
  emptyValue: T,
): T {
  return currentScope && valueScope === currentScope ? value : emptyValue;
}

export function isExtractionPollingStatus(status: unknown): boolean {
  return [
    "PENDING",
    "QUEUED",
    "RUNNING",
    "STARTING",
    "CANCEL_REQUESTED",
  ].includes(String(status || "").toUpperCase());
}

export interface WorkspaceScopedData {
  documents: SorDocument[];
  knowledge: KnowledgeDocument[];
  processingProfile: ProjectProcessingProfile | null;
  workspace: WorkspacePayload | null;
  requirementSets: RequirementSetsPayload | null;
  extraction: ExtractionStatus | null;
  structure: StructurePayload | null;
  structureOptimization: StructureOptimizationPayload | null;
  aiStatus: JsonRecord | null;
  exports: JsonRecord[];
}

export type WorkspaceDataScopeBindings = Record<
  keyof WorkspaceScopedData,
  string
>;

export interface ActiveWorkspaceScopes {
  project: string;
  document: string;
  requirementSet: string;
}

const EMPTY_DOCUMENTS: SorDocument[] = [];
const EMPTY_KNOWLEDGE: KnowledgeDocument[] = [];
const EMPTY_EXPORTS: JsonRecord[] = [];

export function bindWorkspaceDataToScope(
  data: WorkspaceScopedData,
  bindings: WorkspaceDataScopeBindings,
  current: ActiveWorkspaceScopes,
): WorkspaceScopedData {
  return {
    documents: exactScopeValue(
      data.documents,
      bindings.documents,
      current.project,
      EMPTY_DOCUMENTS,
    ),
    knowledge: exactScopeValue(
      data.knowledge,
      bindings.knowledge,
      current.project,
      EMPTY_KNOWLEDGE,
    ),
    processingProfile: exactScopeValue(
      data.processingProfile,
      bindings.processingProfile,
      current.project,
      null,
    ),
    workspace: exactScopeValue(
      data.workspace,
      bindings.workspace,
      current.requirementSet,
      null,
    ),
    requirementSets: exactScopeValue(
      data.requirementSets,
      bindings.requirementSets,
      current.document,
      null,
    ),
    extraction: exactScopeValue(
      data.extraction,
      bindings.extraction,
      current.document,
      null,
    ),
    structure: exactScopeValue(
      data.structure,
      bindings.structure,
      current.document,
      null,
    ),
    structureOptimization: exactScopeValue(
      data.structureOptimization,
      bindings.structureOptimization,
      current.document,
      null,
    ),
    aiStatus: exactScopeValue(
      data.aiStatus,
      bindings.aiStatus,
      current.document,
      null,
    ),
    exports: exactScopeValue(
      data.exports,
      bindings.exports,
      current.project,
      EMPTY_EXPORTS,
    ),
  };
}

export interface WorkspaceContextValue {
  route: WorkspaceRoute;
  updateRoute: (
    patch: Partial<WorkspaceRoute>,
    options?: { replace?: boolean },
  ) => void;
  projects: Project[];
  documents: SorDocument[];
  knowledge: KnowledgeDocument[];
  workspace: WorkspacePayload | null;
  requirementSets: RequirementSetsPayload | null;
  extraction: ExtractionStatus | null;
  structure: StructurePayload | null;
  structureOptimization: StructureOptimizationPayload | null;
  aiStatus: JsonRecord | null;
  exports: JsonRecord[];
  readiness: RuntimeReadiness | null;
  aiSetup: AiProviderSetup | null;
  runtimeCapabilities: RuntimeCapability[];
  processingProfile: ProjectProcessingProfile | null;
  loading: boolean;
  refreshing: boolean;
  error: Error | null;
  notice: string;
  busyAction: string;
  selectedProject: Project | null;
  selectedDocument: SorDocument | null;
  refreshAll: () => Promise<void>;
  refreshProject: () => Promise<void>;
  refreshDocument: () => Promise<void>;
  refreshExtraction: () => Promise<void>;
  refreshStructure: () => Promise<void>;
  refreshStructureOptimization: () => Promise<void>;
  refreshAi: () => Promise<void>;
  refreshExports: () => Promise<void>;
  runAction: <T>(
    label: string,
    action: () => Promise<T>,
    options?: {
      refresh?: "all" | "project" | "document" | "none";
      scope?: "project" | "document" | "requirementSet";
      successNotice?: string;
      failureNotice?: string;
    },
  ) => Promise<T | null>;
  setNotice: (notice: string) => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({
  value,
  children,
}: PropsWithChildren<{ value: WorkspaceContextValue }>) {
  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace(): WorkspaceContextValue {
  const context = useContext(WorkspaceContext);
  if (!context) {
    throw new Error("useWorkspace must be used inside WorkspaceProvider");
  }
  return context;
}
