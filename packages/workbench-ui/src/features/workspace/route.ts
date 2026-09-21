import { useCallback, useEffect, useMemo } from "react";
import { navigate, useBrowserLocation } from "../../shared/navigation";

export type WorkspaceTask =
  | "projects"
  | "reading"
  | "extraction"
  | "analysis"
  | "export"
  | "knowledge";
export type ExtractionMode = "organize" | "review";
export type ExtractionCheckpoint =
  | "diagnose"
  | "structure"
  | "list"
  | "review";

export interface WorkspaceRoute {
  task: WorkspaceTask;
  mode: ExtractionMode;
  checkpoint?: ExtractionCheckpoint;
  projectId: string;
  documentId: string;
  requirementSetId: string;
  reviewCase?: string;
  pluginSessionId?: string;
  resultRevision?: string;
  knowledgeSpace?: string;
  knowledgePage?: string;
  knowledgeView?: "doc" | "graph" | "split";
}

const tasks = new Set<WorkspaceTask>([
  "projects",
  "reading",
  "extraction",
  "analysis",
  "export",
  "knowledge",
]);
const extractionModes = new Set<ExtractionMode>(["organize", "review"]);
const extractionCheckpoints = new Set<ExtractionCheckpoint>([
  "diagnose",
  "structure",
  "list",
  "review",
]);

function compatibleCheckpoint(
  mode: ExtractionMode,
  value: unknown,
): ExtractionCheckpoint | undefined {
  const checkpoint = String(value || "").toLowerCase() as ExtractionCheckpoint;
  if (!extractionCheckpoints.has(checkpoint)) return undefined;
  if (mode === "review") return checkpoint === "review" ? checkpoint : undefined;
  return checkpoint === "review" ? undefined : checkpoint;
}

function legacyTask(params: URLSearchParams): WorkspaceTask {
  if (params.get("knowledgeSpace") || params.get("knowledgePage") || params.get("knowledgeView")) {
    return "knowledge";
  }
  const legacyView = String(params.get("view") || "").toLowerCase();
  const legacyStage = String(params.get("stage") || "").toLowerCase();
  if (legacyView === "projects" || legacyView === "documents") return "projects";
  if (legacyView === "exports") return "export";
  if (legacyStage === "ai" || legacyStage === "matrix") return "analysis";
  if (legacyView === "workspace" || legacyStage === "prepare" || legacyStage === "review") {
    return "extraction";
  }
  return params.get("documentId") ? "extraction" : "projects";
}

export function parseWorkspaceRoute(search: string): WorkspaceRoute {
  const params = new URLSearchParams(search);
  const rawTask = params.get("task") as WorkspaceTask | null;
  const rawMode = params.get("mode") as ExtractionMode | null;
  const task = rawTask && tasks.has(rawTask) ? rawTask : legacyTask(params);
  const mode =
    task === "reading"
      ? "organize"
      : rawMode && extractionModes.has(rawMode)
        ? rawMode
        : String(params.get("stage") || "").toLowerCase() === "review"
          ? "review"
          : params.get("requirementSetId")
            ? "review"
            : "organize";
  const checkpoint =
    task === "extraction"
      ? compatibleCheckpoint(mode, params.get("checkpoint")) ||
        (mode === "organize" ? "diagnose" : undefined)
      : undefined;

  const rawKView = params.get("knowledgeView");
  const knowledgeView =
    rawKView === "doc" || rawKView === "graph" || rawKView === "split"
      ? rawKView
      : undefined;

  return {
    task,
    mode,
    ...(params.get("reviewCase") ? { reviewCase: params.get("reviewCase")! } : {}),
    ...(["extraction", "analysis", "export"].includes(task) && !params.get("reviewCase") && !params.get("requirementSetId") && Boolean(params.get("pluginSessionId")) ? { pluginSessionId: params.get("pluginSessionId")! } : {}),
    ...(["extraction", "analysis", "export"].includes(task) && !params.get("reviewCase") && !params.get("requirementSetId") && /^plugin-[0-9a-f]{32}$/.test(params.get("pluginSessionId") || "") && Boolean(params.get("resultRevision")) ? { resultRevision: params.get("resultRevision")! } : {}),
    ...(checkpoint ? { checkpoint } : {}),
    projectId: params.get("projectId") || "",
    documentId: params.get("documentId") || "",
    requirementSetId:
      task === "reading" ? "" : params.get("requirementSetId") || "",
    ...((task === "knowledge" || params.get("knowledgeSpace")) && params.get("knowledgeSpace")
      ? { knowledgeSpace: params.get("knowledgeSpace")! }
      : {}),
    ...((task === "knowledge" || params.get("knowledgePage")) && params.get("knowledgePage")
      ? { knowledgePage: params.get("knowledgePage")! }
      : {}),
    ...((task === "knowledge" || knowledgeView) && knowledgeView ? { knowledgeView } : {}),
  };
}

export function serializeWorkspaceRoute(route: WorkspaceRoute): string {
  const params = new URLSearchParams();
  params.set("task", route.task);
  if (route.reviewCase) params.set("reviewCase", route.reviewCase);
  if (route.pluginSessionId && ["extraction", "analysis", "export"].includes(route.task) && !route.reviewCase && !route.requirementSetId) params.set("pluginSessionId", route.pluginSessionId);
  if (params.has("pluginSessionId") && route.resultRevision) params.set("resultRevision", route.resultRevision);
  if (route.task === "extraction") {
    params.set("mode", route.mode);
    const checkpoint =
      compatibleCheckpoint(route.mode, route.checkpoint) ||
      (route.mode === "organize" ? "diagnose" : undefined);
    if (checkpoint) params.set("checkpoint", checkpoint);
  }
  if (route.projectId) params.set("projectId", route.projectId);
  if (route.documentId) params.set("documentId", route.documentId);
  if (route.task !== "reading" && route.requirementSetId) {
    params.set("requirementSetId", route.requirementSetId);
  }
  if (route.task === "knowledge" || route.knowledgeSpace || route.knowledgePage) {
    if (route.knowledgeSpace) params.set("knowledgeSpace", route.knowledgeSpace);
    if (route.knowledgePage) params.set("knowledgePage", route.knowledgePage);
    if (route.knowledgeView) params.set("knowledgeView", route.knowledgeView);
  }
  return `/workspace?${params.toString()}`;
}

export function applyWorkspaceRoutePatch(route: WorkspaceRoute, patch: Partial<WorkspaceRoute>): WorkspaceRoute {
  const next = { ...route, ...patch };
  const projectChanged = patch.projectId !== undefined && patch.projectId !== route.projectId;
  const documentChanged = patch.documentId !== undefined && patch.documentId !== route.documentId;
  if (projectChanged || documentChanged) {
    if (projectChanged && patch.documentId === undefined) next.documentId = "";
    if (patch.requirementSetId === undefined) next.requirementSetId = "";
    if (patch.reviewCase === undefined) delete next.reviewCase;
    if (patch.pluginSessionId === undefined) delete next.pluginSessionId;
    if (patch.resultRevision === undefined) delete next.resultRevision;
    if (patch.checkpoint === undefined) delete next.checkpoint;
  }
  if (Object.prototype.hasOwnProperty.call(patch, "pluginSessionId") && patch.pluginSessionId !== route.pluginSessionId && patch.resultRevision === undefined) delete next.resultRevision;
  return next;
}

export function useWorkspaceRoute() {
  const location = useBrowserLocation();
  const route = useMemo(
    () => parseWorkspaceRoute(location.search),
    [location.search],
  );
  const canonicalPath = useMemo(
    () => serializeWorkspaceRoute(route),
    [route],
  );

  useEffect(() => {
    if (
      location.pathname === "/workspace" &&
      `${location.pathname}${location.search}` !== canonicalPath
    ) {
      navigate(canonicalPath, { replace: true });
    }
  }, [canonicalPath, location.pathname, location.search]);

  const update = useCallback(
    (
      patch: Partial<WorkspaceRoute>,
      options: { replace?: boolean } = {},
    ) => {
      const nextPath = serializeWorkspaceRoute(applyWorkspaceRoutePatch(route, patch));
      if (nextPath === serializeWorkspaceRoute(route)) return;
      navigate(nextPath, {
        replace: options.replace,
      });
    },
    [route],
  );

  return { route, update };
}
