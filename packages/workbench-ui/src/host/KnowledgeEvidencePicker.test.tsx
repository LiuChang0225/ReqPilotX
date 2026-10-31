import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApplicationHostContext, type WorkbenchApplicationHost } from "./ApplicationHost";
import { KnowledgeEvidencePicker } from "./KnowledgeEvidencePicker";

function host(): WorkbenchApplicationHost {
  const knowledge = {
    available: true,
    inventory: vi.fn(async () => ({ available: true, projectLibrary: { id: "kb-project", name: "项目参考" },
      projectDocuments: [{ id: "p-doc", name: "internal.txt", coverage: "complete" as const, wiki: "not-requested" }],
      teamLibraries: [{ id: "kb-team", name: "团队规范", documents: [{ id: "t-doc", name: "team.md", coverage: "complete" as const, wiki: "not-requested" }] }] })),
    uploadReference: vi.fn(async () => ({})),
    retryReference: vi.fn(async () => ({})),
    teamTopics: vi.fn(async () => [{ version_id: "topic-v1", label: "设计主题", availability: "AVAILABLE", review_state: "VERIFIED" }]),
    teamLibraries: vi.fn(async () => []),
  };
  return { id: "host", label: "PAIR", modelLabel: "model", captureAvailable: false, taskStartAvailable: true,
    resourceUrl: path => path, pdfViewerUrl: source => source, knowledge,
    startTask: vi.fn(), restoreTask: vi.fn(async () => null), taskDraft: vi.fn(), controlTask: vi.fn(), openSession: vi.fn() };
}

describe("KnowledgeEvidencePicker", () => {
  it("selects only explicit project and retained-topic evidence", async () => {
    const value = host(); const onChange = vi.fn();
    const { unmount } = render(<ApplicationHostContext.Provider value={value}><KnowledgeEvidencePicker projectId="p" onChange={onChange} /></ApplicationHostContext.Provider>);
    await screen.findByText("internal.txt");
    await userEvent.click(screen.getByRole("checkbox", { name: /internal.txt/ }));
    await userEvent.selectOptions(screen.getByLabelText("选择团队知识库"), "kb-team");
    await userEvent.click(screen.getByRole("button", { name: "保留主题版本" }));
    await screen.findByText("设计主题");
    await userEvent.click(screen.getByRole("radio"));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      mode: "COMBINED", projectDocumentIds: ["p-doc"],
      team: expect.objectContaining({ kbId: "kb-team", topicVersionId: "topic-v1" }),
    })));
    expect(value.knowledge?.teamTopics).toHaveBeenCalledWith("kb-team");
    unmount();
  });

  it("uploads a project reference through the Host rather than a generic JSON form", async () => {
    const value = host();
    const { container } = render(<ApplicationHostContext.Provider value={value}><KnowledgeEvidencePicker projectId="p" /></ApplicationHostContext.Provider>);
    await screen.findByText("internal.txt");
    const file = new File(["internal source"], "new-reference.txt", { type: "text/plain" });
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(value.knowledge?.uploadReference).toHaveBeenCalledWith("p", file, "REFERENCE"));
  });
});
