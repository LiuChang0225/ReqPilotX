/* Reuse the main workspace, tree, content renderer, PDF viewer and Inspector.
   This provider binds an authored candidate artifact, without creating a second UI. */
import { useCallback, useEffect, useState } from "react";
import { request } from "../../shared/api/client";
import type { Project, RequirementSetsPayload, SorDocument } from "../../shared/api/contracts";
import { ErrorBlock, LoadingBlock } from "../../shared/ui/Feedback";
import { WorkspaceProvider, type WorkspaceContextValue } from "../workspace/WorkspaceContext";
import { WorkspaceShell } from "../workspace/WorkspaceShell";
import { useWorkspaceRoute } from "../workspace/route";
import { RequirementTreePanel } from "./RequirementTreePanel";

interface CandidateBootstrap {
  caseId: string; project: Project; document: SorDocument;
  requirementSets: RequirementSetsPayload; scope: string;
}
const noOperation = async () => {};
export function CandidateReviewWorkspace() {
  const { route, update } = useWorkspaceRoute();
  const [data, setData] = useState<CandidateBootstrap | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    try { setData(await request<CandidateBootstrap>("/api/candidate-review")); setError(null); }
    catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  if (error) return <ErrorBlock error={error} onRetry={() => void load()} />;
  if (!data) return <LoadingBlock label="正在载入原文核对候选" />;
  if (route.reviewCase !== data.caseId) return <ErrorBlock error={new Error("候选标识与当前文档不一致，请从当前核对入口打开。")} onRetry={() => void load()} />;
  const context: WorkspaceContextValue = {
    route: { ...route, task: "extraction", mode: "review", projectId: data.project.id,
      documentId: data.document.id, requirementSetId: data.requirementSets.active_requirement_set_id || "" },
    updateRoute: (patch, options) => {
      if ((patch.task && patch.task !== "extraction") || patch.mode === "organize") {
        setNotice("当前入口只核对此份原文候选；其他业务任务在常规项目入口继续。"); return;
      }
      update({ ...patch, reviewCase: data.caseId }, options);
    },
    projects: [data.project], documents: [data.document], selectedProject: data.project,
    selectedDocument: data.document, requirementSets: data.requirementSets,
    workspace: { project: data.project, document: data.document }, knowledge: [], extraction: null,
    structure: null, structureOptimization: null, aiStatus: null, exports: [], readiness: null,
    aiSetup: null, runtimeCapabilities: [], processingProfile: null, loading: false, refreshing: false,
    error: null, notice, busyAction: "", setNotice,
    refreshAll: load, refreshProject: load, refreshDocument: load,
    refreshExtraction: noOperation, refreshStructure: noOperation,
    refreshStructureOptimization: noOperation, refreshAi: noOperation, refreshExports: noOperation,
    runAction: async (_label, action) => { try { return await action(); } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : String(cause)); return null;
    } },
  };
  return <WorkspaceProvider value={context}><WorkspaceShell><RequirementTreePanel /></WorkspaceShell></WorkspaceProvider>;
}
