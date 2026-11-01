import { afterEach, describe, expect, it, vi } from "vitest";
import { requestWithTransport } from "../../../../packages/workbench-ui/src/shared/api/client";
import { setSaasCsrfToken, saasCsrfFor } from "../../../../packages/workbench-ui/src/shared/api/saasSession";

afterEach(() => { setSaasCsrfToken(null); vi.restoreAllMocks(); });

describe("independent authenticated request transport acceptance", () => {
  it("attaches CSRF only to same-origin API mutations", async () => {
    setSaasCsrfToken("fixture-csrf");
    const transport = vi.fn<typeof fetch>().mockImplementation(async () => new Response("{}", { headers: { "Content-Type": "application/json" } }));
    await requestWithTransport(transport, "/api/projects", { method: "POST", body: "{}" });
    expect(new Headers(transport.mock.calls[0][1]?.headers).get("X-ReqPilot-CSRF")).toBe("fixture-csrf");
    await requestWithTransport(transport, "/api/projects");
    expect(new Headers(transport.mock.calls[1][1]?.headers).has("X-ReqPilot-CSRF")).toBe(false);
    await requestWithTransport(transport, "https://foreign.example.invalid/api/projects", { method: "POST", body: "{}" });
    expect(new Headers(transport.mock.calls[2][1]?.headers).has("X-ReqPilot-CSRF")).toBe(false);
    expect(transport.mock.calls[2][1]?.credentials).toBe("same-origin");
    expect(saasCsrfFor("//foreign.example.invalid/api/projects")).toBeNull();
    expect(saasCsrfFor("/non-api/page")).toBeNull();
  });

  it("clears expired authentication even if HTTP401 has malformed JSON", async () => {
    setSaasCsrfToken("fixture-csrf");
    const expired = vi.fn(); window.addEventListener("reqpilotx:session-expired", expired);
    try {
      const transport = vi.fn<typeof fetch>().mockResolvedValue(new Response("broken-json", { status: 401, headers: { "Content-Type": "application/json" } }));
      await expect(requestWithTransport(transport, "/api/projects")).rejects.toThrow();
      expect(expired).toHaveBeenCalledOnce();
      expect(saasCsrfFor("/api/projects")).toBeNull();
    } finally { window.removeEventListener("reqpilotx:session-expired", expired); }
  });

  it("does not let a late prior-account HTTP401 expire a newer session", async () => {
    setSaasCsrfToken("fixture-account-a");
    let resolve!: (response: Response) => void;
    const transport = vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>((done) => { resolve = done; }));
    const request = requestWithTransport(transport, "/api/projects");
    setSaasCsrfToken("fixture-account-b");
    const expired = vi.fn(); window.addEventListener("reqpilotx:session-expired", expired);
    try {
      resolve(new Response('{"detail":"session_expired"}', { status: 401, headers: { "Content-Type": "application/json" } }));
      await expect(request).rejects.toThrow();
      expect(expired).not.toHaveBeenCalled();
      expect(saasCsrfFor("/api/projects")).toBe("fixture-account-b");
    } finally { window.removeEventListener("reqpilotx:session-expired", expired); }
  });
});
