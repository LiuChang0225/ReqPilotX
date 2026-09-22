import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  applyWorkspaceRoutePatch,
  useWorkspaceRoute,
  parseWorkspaceRoute,
  serializeWorkspaceRoute,
} from "./route";

describe("workspace route", () => {
  it("keeps the bound plugin draft on document tasks but does not trap project or reading navigation", () => {
    const session = "plugin-" + "a".repeat(32);
    const route = parseWorkspaceRoute(`?task=extraction&projectId=p&documentId=d&pluginSessionId=${session}`);
    expect(route.pluginSessionId).toBe(session);
    expect(serializeWorkspaceRoute({ ...route, task: "projects" })).not.toContain("pluginSessionId");
    expect(parseWorkspaceRoute(`?task=reading&pluginSessionId=${session}`).pluginSessionId).toBeUndefined();
    expect(parseWorkspaceRoute(`?task=extraction&requirementSetId=current&pluginSessionId=${session}`).pluginSessionId).toBeUndefined();
  });
  it("keeps reading as a peer task with only the current document identity", () => {
    const route = parseWorkspaceRoute(
      "?task=reading&mode=review&checkpoint=list&projectId=p1&documentId=d1&requirementSetId=s1",
    );

    expect(route).toEqual({
      task: "reading",
      mode: "organize",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "",
    });
    expect(serializeWorkspaceRoute(route)).toBe(
      "/workspace?task=reading&projectId=p1&documentId=d1",
    );
  });

  it("canonicalizes an organize entry without a checkpoint to parsing diagnosis", () => {
    const route = parseWorkspaceRoute(
      "?task=extraction&mode=organize&projectId=p1&documentId=d1",
    );

    expect(route).toEqual({
      task: "extraction",
      mode: "organize",
      checkpoint: "diagnose",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "",
    });
    expect(serializeWorkspaceRoute({ ...route, checkpoint: undefined })).toBe(
      "/workspace?task=extraction&mode=organize&checkpoint=diagnose&projectId=p1&documentId=d1",
    );
  });

  it("uses the canonical extraction task contract", () => {
    expect(
      parseWorkspaceRoute(
        "?task=extraction&mode=review&projectId=p1&documentId=d1&requirementSetId=s1",
      ),
    ).toEqual({
      task: "extraction",
      mode: "review",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "s1",
    });
  });

  it("round-trips an extraction checkpoint without changing its data mode", () => {
    const route = parseWorkspaceRoute(
      "?task=extraction&mode=organize&checkpoint=list&projectId=p1&documentId=d1",
    );

    expect(route).toEqual({
      task: "extraction",
      mode: "organize",
      checkpoint: "list",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "",
    });
    expect(serializeWorkspaceRoute(route)).toBe(
      "/workspace?task=extraction&mode=organize&checkpoint=list&projectId=p1&documentId=d1",
    );
  });

  it("ignores checkpoints that conflict with the extraction data boundary", () => {
    expect(
      parseWorkspaceRoute(
        "?task=extraction&mode=review&checkpoint=structure&requirementSetId=s1",
      ),
    ).toEqual({
      task: "extraction",
      mode: "review",
      projectId: "",
      documentId: "",
      requirementSetId: "s1",
    });
    expect(
      parseWorkspaceRoute(
        "?task=extraction&mode=organize&checkpoint=review&requirementSetId=s1",
      ),
    ).toMatchObject({ mode: "organize" });
    expect(
      parseWorkspaceRoute(
        "?task=extraction&mode=organize&checkpoint=review&requirementSetId=s1",
      ),
    ).toHaveProperty("checkpoint", "diagnose");
  });

  it("serializes checkpoint only inside the extraction task", () => {
    const path = serializeWorkspaceRoute({
      task: "analysis",
      mode: "organize",
      checkpoint: "list",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "s1",
    });

    expect(path).toBe(
      "/workspace?task=analysis&projectId=p1&documentId=d1&requirementSetId=s1",
    );
    expect(path).not.toContain("checkpoint=");
  });

  it("maps old URLs into one canonical destination without retaining old UI state", () => {
    expect(
      parseWorkspaceRoute(
        "?view=workspace&stage=prepare&prepareView=structure&projectId=p1&documentId=d1",
      ),
    ).toEqual({
      task: "extraction",
      mode: "organize",
      checkpoint: "diagnose",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "",
    });
    expect(
      parseWorkspaceRoute("?view=documents&projectId=p1"),
    ).toMatchObject({ task: "projects", mode: "organize" });
  });

  it("serializes only task, extraction mode and business context", () => {
    const path = serializeWorkspaceRoute({
      task: "extraction",
      mode: "review",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "s1",
    });
    expect(path).toBe(
      "/workspace?task=extraction&mode=review&projectId=p1&documentId=d1&requirementSetId=s1",
    );
    expect(path).not.toContain("view=");
    expect(path).not.toContain("stage=");
    expect(path).not.toContain("prepareView=");
  });

  it("remembers confirmation while visiting another task through the frozen set identity", () => {
    const outsideExtraction = serializeWorkspaceRoute({
      task: "projects",
      mode: "review",
      projectId: "p1",
      documentId: "d1",
      requirementSetId: "s1",
    });

    expect(outsideExtraction).not.toContain("mode=");
    expect(parseWorkspaceRoute(outsideExtraction.split("?")[1])).toMatchObject({
      task: "projects",
      mode: "review",
      requirementSetId: "s1",
    });
  });
});
it("preserves a viewed external revision across document tasks and does not silently replace an invalid version",()=>{
 const session="plugin-"+"a".repeat(32),revision="b".repeat(64);
 const route=parseWorkspaceRoute(`?task=extraction&projectId=p&documentId=d&pluginSessionId=${session}&resultRevision=${revision}`);
 expect(parseWorkspaceRoute(serializeWorkspaceRoute({...route,task:"export"}).split("?")[1]).resultRevision).toBe(revision);
 expect(serializeWorkspaceRoute({...route,task:"projects"})).not.toContain("resultRevision");
 expect(parseWorkspaceRoute(`?task=extraction&pluginSessionId=${session}&resultRevision=unknown`).resultRevision).toBe("unknown");
});

it("clears prior document bindings when selecting a different document or project", () => {
  const route = parseWorkspaceRoute(`?task=extraction&projectId=p&documentId=d&checkpoint=list&pluginSessionId=plugin-${"a".repeat(32)}&resultRevision=${"b".repeat(64)}`);
  expect(applyWorkspaceRoutePatch(route, { documentId: "new" })).toMatchObject({ documentId: "new", requirementSetId: "" });
  expect(applyWorkspaceRoutePatch(route, { documentId: "new" })).not.toHaveProperty("pluginSessionId");
  expect(applyWorkspaceRoutePatch(route, { documentId: "new" })).not.toHaveProperty("resultRevision");
  expect(applyWorkspaceRoutePatch(route, { documentId: "new" })).not.toHaveProperty("checkpoint");
  expect(applyWorkspaceRoutePatch({ ...route, requirementSetId: "legacy-set", reviewCase: "old" }, { projectId: "new-project" })).toMatchObject({ documentId: "", requirementSetId: "" });
  expect(applyWorkspaceRoutePatch({ ...route, requirementSetId: "legacy-set", reviewCase: "old" }, { projectId: "new-project" })).not.toHaveProperty("reviewCase");
  expect(applyWorkspaceRoutePatch(route, { documentId: "new", pluginSessionId: "plugin-" + "c".repeat(32), resultRevision: "d".repeat(64), checkpoint: "structure" })).toMatchObject({ pluginSessionId: "plugin-" + "c".repeat(32), resultRevision: "d".repeat(64), checkpoint: "structure" });
});

it("keeps invalid external session and version identities visible instead of switching the producer", () => {
  const parsed = parseWorkspaceRoute("?task=analysis&projectId=p&documentId=d&pluginSessionId=unavailable-session&resultRevision=invalid");
  expect(parsed.pluginSessionId).toBe("unavailable-session");
  expect(serializeWorkspaceRoute(parsed)).toContain("pluginSessionId=unavailable-session");
});

it("restores the exact external version and extraction checkpoint on browser back and forward", async () => {
  const original = `/workspace?task=extraction&pluginSessionId=plugin-${"a".repeat(32)}&resultRevision=${"b".repeat(64)}&mode=organize&checkpoint=list&projectId=p&documentId=d`;
  window.history.replaceState(null, "", original);
  const { result, unmount } = renderHook(() => useWorkspaceRoute());
  act(() => result.current.update({ task: "analysis" }));
  expect(result.current.route.task).toBe("analysis");
  act(() => window.history.back());
  await waitFor(() => expect(result.current.route.task).toBe("extraction"));
  expect(result.current.route.checkpoint).toBe("list");
  expect(result.current.route.resultRevision).toBe("b".repeat(64));
  act(() => window.history.forward());
  await waitFor(() => expect(result.current.route.task).toBe("analysis"));
  expect(result.current.route.resultRevision).toBe("b".repeat(64));
  const push = vi.spyOn(window.history, "pushState");
  act(() => result.current.update({ task: "analysis" }));
  expect(push).not.toHaveBeenCalled();
  unmount();
  push.mockRestore();
});
