import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DocumentWorkPicker } from "./DocumentWorkPicker";
import { ApplicationHostContext, type WorkbenchApplicationHost } from "../../host/ApplicationHost";

vi.mock("./documentWorkCatalog", () => ({
  useDocumentWorkCatalog: () => ({ value: { sessions: [{ session_id: "plugin-" + "a".repeat(32), project_id: "p",
    document_id: "d", source_sha256: "b".repeat(64), revision_id: null, revision_number: 0, stages: [],
    goal: "", updated_at: "2026-10-31T00:00:00Z", title: "", availability: "NO_ARTIFACTS" }], unavailable: [] },
  loading: false, error: "", refresh: () => {} }),
  selectedPluginWork: vi.fn(),
}));
afterEach(cleanup);
const props = { projectId: "p", documentId: "d", sourceSha256: "b".repeat(64), selectedSession: "plugin-" + "a".repeat(32), summary: null, onSelect: () => {} };
const host = { id: "pair", label: "PAIR" } as WorkbenchApplicationHost;
it("uses the native host task name in a PAIR-hosted view and keeps external wording standalone", () => {
  const view = render(<ApplicationHostContext.Provider value={host}><DocumentWorkPicker {...props} /></ApplicationHostContext.Provider>);
  expect(screen.getByText(/PAIR 任务/)).toBeVisible();
  expect(screen.queryByText(/外部任务/)).not.toBeInTheDocument();
  view.rerender(<DocumentWorkPicker {...props} />);
  expect(screen.getByText(/外部任务/)).toBeVisible();
});
