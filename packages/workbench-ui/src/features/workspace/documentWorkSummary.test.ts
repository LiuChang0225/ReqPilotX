import { afterEach, expect, it, vi } from "vitest";
import { readDocumentWorkSummary } from "./documentWorkSummary";

afterEach(() => vi.unstubAllGlobals());

it("reads an explicit empty native task as NOT_STARTED instead of a failed task status", async () => {
  const source = "a".repeat(64);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/loop/latest")) return new Response(JSON.stringify({
      schema_version: "source-document-loop/v1", task_id: null, job_status: "NOT_STARTED",
      job_availability: "UNAVAILABLE_LOCAL_RUNTIME" }), { headers: { "content-type": "application/json" } });
    return new Response(JSON.stringify({ detail: "原文条目尚未生成" }), {
      status: 404, headers: { "content-type": "application/json" } });
  }));
  const value = await readDocumentWorkSummary("p", "d", source, new AbortController().signal);
  expect(value?.task).toBeNull();
  expect(value?.taskStatus).toBe("NOT_STARTED");
  expect(value?.taskError).toBe("");
  expect(value?.deliveryError).toBe("");
});
