import { useContext, useEffect } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { WorkbenchHostContext } from "../features/workspace/WorkbenchHostContext";
import { ApplicationHostContext, type WorkbenchApplicationHost } from "./ApplicationHost";
import { ReqPilotXApplication } from "./ReqPilotXApplication";

const observed = vi.hoisted(() => ({ presentationChanged: vi.fn() }));
vi.mock("../features/home/HomePage", () => ({ HomePage: function Home() {
  const presentation = useContext(WorkbenchHostContext), host = useContext(ApplicationHostContext)!;
  useEffect(() => { observed.presentationChanged(presentation); }, [presentation]);
  return <p>{host.modelLabel}</p>;
} }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); observed.presentationChanged.mockClear(); });

it("updates task configuration without changing the business presentation supplied to readers", () => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  const host: WorkbenchApplicationHost = { id: "presentation-host", label: "PAIR", modelLabel: "initial model", captureAvailable: false, taskStartAvailable: false,
    resourceUrl: path => path, pdfViewerUrl: source => source, restoreTask: async () => null, taskDraft: () => undefined,
    startTask: vi.fn(), controlTask: vi.fn(), openSession: async () => {} };
  const view = render(<ReqPilotXApplication host={host} />);
  expect(observed.presentationChanged).toHaveBeenCalledTimes(1);
  view.rerender(<ReqPilotXApplication host={{ ...host, modelLabel: "ready model", taskStartAvailable: true, captureAvailable: true }} />);
  expect(screen.getByText("ready model")).toBeVisible();
  expect(observed.presentationChanged).toHaveBeenCalledTimes(1);
  view.rerender(<ReqPilotXApplication host={{ ...host, resourceUrl: path => "/new-transport" + path }} />);
  expect(observed.presentationChanged).toHaveBeenCalledTimes(2);
});
