import { afterEach, beforeEach, expect, it, vi } from "vitest";

beforeEach(() => vi.resetModules());
afterEach(() => vi.restoreAllMocks());

it("routes embedded requests and resources without replacing the host window's fetch", async () => {
  const browserFetch = vi.spyOn(globalThis, "fetch");
  const { applicationFetch, applicationResourceUrl, installApplicationTransport } = await import("./applicationTransport");
  const hostFetch = vi.fn<typeof fetch>(async () => new Response("saved"));
  const dispose = installApplicationTransport({ id: "fixture", fetch: hostFetch, resourceUrl: path => "/reqpilotx/service" + path });
  expect(applicationResourceUrl("/api/projects")).toBe("/reqpilotx/service/api/projects");
  await applicationFetch("/api/projects");
  expect(browserFetch).not.toHaveBeenCalled();
  expect(hostFetch).toHaveBeenCalledOnce();
  expect(globalThis.fetch).toBe(browserFetch);
  dispose();
});

it("aborts its own generation and refuses browser fallback after disposal", async () => {
  const browserFetch = vi.spyOn(globalThis, "fetch");
  const { applicationFetch, installApplicationTransport } = await import("./applicationTransport");
  let signal: AbortSignal | null | undefined;
  const dispose = installApplicationTransport({ id: "fixture", resourceUrl: path => path, fetch: async (_input, init) => {
    signal = init?.signal; return new Response("saved");
  } });
  await applicationFetch("/api/projects");
  dispose();
  expect(signal?.aborted).toBe(true);
  expect(() => applicationFetch("/api/projects")).toThrow("连接已关闭");
  expect(browserFetch).not.toHaveBeenCalled();
});

it("keeps standalone Web requests on their normal browser transport", async () => {
  const browserFetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("local"));
  const { applicationFetch, applicationResourceUrl } = await import("./applicationTransport");
  expect(applicationResourceUrl("/api/projects")).toBe("/api/projects");
  await applicationFetch("/api/projects");
  expect(browserFetch).toHaveBeenCalledWith("/api/projects", undefined);
});
