import { expect, it } from "vitest";
import { hostedCoreRoute } from "./hostedCoreRoute";
import type { WorkspaceRoute } from "./route";

const base: WorkspaceRoute = { task: "projects", mode: "organize", projectId: "p", documentId: "d", requirementSetId: "" };

it("routes a legacy reading link to extraction with the same project and document", () => {
  expect(hostedCoreRoute({ ...base, task: "reading" })).toMatchObject({ task: "extraction", projectId: "p", documentId: "d", mode: "organize", checkpoint: "structure" });
});

it("routes a legacy knowledge link to project management without inventing a knowledge page", () => {
  expect(hostedCoreRoute({ ...base, task: "knowledge", knowledgePage: "k" })).toMatchObject({ task: "projects", projectId: "p", documentId: "", requirementSetId: "" });
  expect(hostedCoreRoute({ ...base, task: "knowledge" })).not.toHaveProperty("knowledgePage");
});

it("folds a legacy confirmed-set review into the current extraction list", () => {
  expect(hostedCoreRoute({ ...base, task: "extraction", mode: "review", requirementSetId: "legacy" }))
    .toMatchObject({ task: "extraction", mode: "organize", checkpoint: "list", requirementSetId: "" });
});

it("leaves the four supported PAIR destinations untouched", () => {
  for (const task of ["projects", "extraction", "analysis", "export"] as const) {
    expect(hostedCoreRoute({ ...base, task })).toBeNull();
  }
});
