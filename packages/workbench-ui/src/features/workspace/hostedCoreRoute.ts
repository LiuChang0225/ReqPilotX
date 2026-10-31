import type { WorkspaceRoute } from "./route";

export const hostedCoreTasks = ["projects", "extraction", "analysis", "export"] as const;

/** Fold legacy reading/knowledge links into the four supported PAIR destinations. */
export function hostedCoreRoute(route: WorkspaceRoute): WorkspaceRoute | null {
  if (route.task === "extraction" && route.mode === "review") {
    return { ...route, mode: "organize", checkpoint: "list", requirementSetId: "" };
  }
  if (!["reading", "knowledge"].includes(route.task)) return null;
  if (route.task === "reading" && route.documentId) {
    return { ...route, task: "extraction", mode: "organize", checkpoint: "structure" };
  }
  return { ...route, task: "projects", documentId: "", requirementSetId: "" };
}
