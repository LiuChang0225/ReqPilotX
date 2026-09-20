import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const renderSpies = vi.hoisted(() => ({
  workspace: vi.fn(),
}));

vi.mock("../features/workspace/WorkspacePage", () => ({
  WorkspacePage: () => {
    renderSpies.workspace();
    return <div>workspace-page</div>;
  },
}));

vi.mock("./preferences", () => ({
  PreferencesProvider: ({ children }: { children: ReactNode }) => children,
}));

afterEach(() => {
  cleanup();
  renderSpies.workspace.mockClear();
  window.history.replaceState(null, "", "/");
});

describe("App workspace routing", () => {
  it.each(["/", "/workspace"])(
    "keeps the reading task inside the unified workspace at %s",
    async (pathname) => {
      window.history.replaceState(
        null,
        "",
        `${pathname}?task=reading&projectId=p1&documentId=d1`,
      );

      render(<App />);

      expect(await screen.findByText("workspace-page")).toBeInTheDocument();
      await waitFor(() => expect(renderSpies.workspace).toHaveBeenCalled());
    },
  );
});
