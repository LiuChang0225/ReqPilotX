import { createContext, useContext } from "react";
import type { AgentLoopTask } from "../extraction/SourceItemAgentLoop";
import type { SourceStage } from "../extraction/SourceDocumentStageResults";

export type DocumentAgentBinding = {
  sourceSha256: string;
  baselineSha256: string;
  selectedSourceItemId: string;
  selectedDerivedUnitId?: string;
  selectedLabel?: string;
  selectedNodeId?: string;
  revisionId?: string;
  onUpdated: () => void;
};
export type DocumentAgentContextValue = {
  collaborationAvailable?: boolean;
  producer?: "NATIVE" | "EXTERNAL_PLUGIN";
  bind: (binding: DocumentAgentBinding) => () => void;
  task: AgentLoopTask | null;
  show: (goal?: string, scope?: "selected" | "document") => void;
  openStage: (stage: SourceStage) => void;
  historyHost: HTMLElement | null;
};
export const DocumentAgentContext = createContext<DocumentAgentContextValue | null>(null);
export const useDocumentAgent = () => useContext(DocumentAgentContext);
