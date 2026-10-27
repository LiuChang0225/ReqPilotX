import * as React from "react";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { WorkbenchApplicationHost } from "./ApplicationHost";

const require = createRequire(resolve("package.json"));
const discovery = require("./integrations/pair/host/src/application-discovery.cjs");
const taskSource = readFileSync(resolve("integrations/pair/client/src/task-controller.cjs"), "utf8");
const catalogSource = readFileSync(resolve("integrations/pair/client/src/catalog.cjs"), "utf8");
afterEach(() => { cleanup(); vi.useRealTimers(); localStorage.clear(); });

it("updates a mounted READY application's capabilities and controller without replacing its transport", async () => {
  vi.useFakeTimers();
  const ownerId = "12345678-1234-4234-8234-123456789abc";
  const base = { mode: "managed", status: "READY", ownerId, serviceOrigin: "http://127.0.0.1:19001" };
  let configuration = discovery.publicConfiguration({ ...base, taskModel: { provider: "test", model: "initial-model" } });
  const scope = { projectId: "p", documentId: "d", sourceSha256: "a".repeat(64) };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === "/reqpilotx/config") return Response.json(configuration);
    if (path.startsWith("/reqpilotx/tasks?")) return Response.json({ tasks: [] });
    if (path === "/reqpilotx/tasks/start") {
      const request = JSON.parse(String(init?.body));
      return Response.json({ ...request, schema: "reqpilot-pair-launch/v1", applicationId: configuration.applicationId,
        status: "ACCEPTED", sessionId: "session-one", workspaceId: "workspace-one", workRef: "plugin-" + "b".repeat(32) });
    }
    throw Error(`Unexpected request: ${path}`);
  });
  let current!: WorkbenchApplicationHost;
  const mounted = vi.fn(), disposed = vi.fn(), transportDisposed = vi.fn();
  function Probe({ host }: { host: WorkbenchApplicationHost }) {
    current = host;
    React.useEffect(() => { mounted(); return disposed; }, []);
    return <><span>{host.modelLabel}</span><button disabled={!host.taskStartAvailable}>Start</button><span>Capture: {String(host.captureAvailable)}</span></>;
  }
  const install = vi.fn(() => transportDisposed);
  const taskModule = { exports: {} };
  const controller = new Function("module", "fetch", taskSource + ";return module.exports.createPairTaskController;")(taskModule, fetcher);
  const Application: React.ComponentType<{ t: (key: string) => string; openSession: () => Promise<void> }> = new Function(
    "React", "h", "copy", "module", "panelModule", "connectionIdentity", "assertCompatibleConfiguration", "createReadonlyHttpClient",
    "assetUrl", "createPairTaskController", "PREFIX", "fetch", "Panel",
    catalogSource + ";return Application;",
  )(React, React.createElement, { zh: {}, en: {} }, { exports: {} }, { exports: { ReqPilotXApplication: Probe, installApplicationTransport: install } },
    discovery.connectionIdentity, discovery.assertCompatibleConfiguration, () => ({}), (path: string) => path, controller, "/reqpilotx", fetcher, () => null);
  const view = render(<Application t={key => key} openSession={async () => {}} />);
  await act(async () => {});
  expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  const resource = current.resourceUrl, viewer = current.pdfViewerUrl;
  await current.restoreTask(scope);
  expect(fetcher.mock.calls.filter(([path]) => path.startsWith("/reqpilotx/tasks?"))).toHaveLength(0);

  configuration = discovery.publicConfiguration({ ...base, taskLaunchReady: true, taskLookupAvailable: true, workbenchCapture: true,
    taskModel: { provider: "test", model: "ready-model", reasoningEffort: "xhigh" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
  expect(screen.getByText("ready-model / xhigh")).toBeVisible();
  expect(screen.getByText("Capture: true")).toBeVisible();
  await expect(current.startTask({ ...scope, goal: "Use the current controller" })).resolves.toMatchObject({ sessionId: "session-one" });
  expect(fetcher.mock.calls.some(([path]) => path.startsWith("/reqpilotx/tasks?"))).toBe(true);

  configuration = discovery.publicConfiguration({ ...base, taskModel: { provider: "test", model: "changed-model" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  expect(screen.getByText("changed-model")).toBeVisible();
  expect(screen.getByText("Capture: false")).toBeVisible();
  await expect(current.startTask({ ...scope, goal: "Do not dispatch after revocation" })).rejects.toThrow(/启动能力/);
  const calls = fetcher.mock.calls.length;
  await current.restoreTask(scope);
  expect(fetcher).toHaveBeenCalledTimes(calls);
  expect(current.resourceUrl).toBe(resource); expect(current.pdfViewerUrl).toBe(viewer);
  expect(install).toHaveBeenCalledTimes(1); expect(mounted).toHaveBeenCalledTimes(1);
  expect(transportDisposed).not.toHaveBeenCalled();
  view.unmount();
  expect(transportDisposed).toHaveBeenCalledTimes(1); expect(disposed).toHaveBeenCalledTimes(1);
});
