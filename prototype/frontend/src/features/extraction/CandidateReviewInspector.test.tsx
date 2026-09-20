import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import type { RequirementMatrixNode } from "../../shared/api/contracts";
import { CandidateReviewInspector } from "./CandidateReviewInspector";

vi.mock("../../shared/api/client", async (original) => ({ ...await original<typeof import("../../shared/api/client")>(), request: vi.fn() }));
let revision = 0;
const state = () => ({ revision, sourceRevision: "synthetic-source", writeToken: "synthetic-test-token", targets: {} });
function props(id: string) {
  return { node: { node_id:id, source_number:id, display_title:id, classification_paths:["PROJECT"], topic_tags:[],
    candidate_review:{sourceRevision:"synthetic-source",requirementness:"REQUIREMENT"} } as unknown as RequirementMatrixNode,
    topics:{}, saveRef:{current:null}, resetSignal:0, onSaveState:vi.fn(), onSaved:vi.fn(async () => {}),
    beginMutation:() => ({generation:1,kind:"single" as const,uiScope:"synthetic-scope"}),
    isMutationCurrent:() => true, isTargetCurrent:() => true };
}
beforeEach(() => { revision=0; vi.mocked(request).mockImplementation(async () => state() as never); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
describe("candidate feedback lifetime", () => {
  it("shows current and previous judgments without promoting historical acceptance", async () => {
    const values=props("native-diff-case");
    values.node.candidate_review={sourceRevision:"synthetic-source", requirementness:"NON_REQUIREMENT",
      sourceId:"SRC-0012", contentRole:"REFERENCE", previousDecision:"需求 · 约束", previousRequirementId:"REQ-0012",
      previousReason:"An old judgment", reason:"Retained reference list", openIssues:[{id:"q1",summary:"Missing source input"}]};
    values.node.classification_paths=[];
    render(<CandidateReviewInspector {...values} />);
    expect(screen.getByRole("region",{name:"前后判定差异"})).toHaveTextContent("SRC-0012");
    expect(screen.getByText("Retained reference list")).toBeInTheDocument();
    expect(screen.getByText(/REQ-0012 保留为历史编号/)).toBeInTheDocument();
    expect(screen.getByRole("region",{name:"尚未解决的来源问题"})).toHaveTextContent("Missing source input");
    await waitFor(() => expect(screen.getByRole("status")).not.toHaveTextContent("本条已核对"));
  });
  it("keeps a cached draft across remounts after an earlier Escape", async () => {
    const values=props("reset-case");
    const first=render(<CandidateReviewInspector {...values} resetSignal={5} />);
    await userEvent.type(screen.getByRole("textbox",{name:"核对反馈"}),"pending draft");
    first.unmount();
    const second=render(<CandidateReviewInspector {...values} resetSignal={5} />);
    expect(screen.getByRole("textbox",{name:"核对反馈"})).toHaveValue("pending draft");
    second.rerender(<CandidateReviewInspector {...values} resetSignal={6} />);
    await waitFor(() => expect(screen.getByRole("textbox",{name:"核对反馈"})).toHaveValue(""));
  });
  it("clears the submitted draft and refreshes the workspace if its editor unmounts during save", async () => {
    let finish!: (value: unknown) => void;
    const committed = new Promise(resolve => { finish=resolve; });
    vi.mocked(request).mockImplementation(async (_url, init) => (init?.method === "POST" ? committed : state()) as never);
    const values=props("save-unmount-case");
    const first=render(<CandidateReviewInspector {...values} />);
    await userEvent.type(screen.getByRole("textbox",{name:"核对反馈"}),"submitted draft");
    await userEvent.click(screen.getByRole("button",{name:"保存反馈"}));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/review",expect.objectContaining({method:"POST"})));
    first.unmount();
    await act(async () => { revision=1; finish({state:state()}); await committed; });
    expect(values.onSaved).toHaveBeenCalledTimes(1);
    render(<CandidateReviewInspector {...values} />);
    expect(screen.getByRole("textbox",{name:"核对反馈"})).toHaveValue("");
  });
  it("does not erase a newer draft when an older save finishes", async () => {
    let finish!: (value: unknown) => void;
    const committed = new Promise(resolve => { finish=resolve; });
    vi.mocked(request).mockImplementation(async (_url, init) => (init?.method === "POST" ? committed : state()) as never);
    const values=props("save-newer-case");
    const first=render(<CandidateReviewInspector {...values} />);
    await userEvent.type(screen.getByRole("textbox",{name:"核对反馈"}),"older draft");
    await userEvent.click(screen.getByRole("button",{name:"保存反馈"}));
    first.unmount();
    const second=render(<CandidateReviewInspector {...values} />);
    await userEvent.clear(screen.getByRole("textbox",{name:"核对反馈"}));
    await userEvent.type(screen.getByRole("textbox",{name:"核对反馈"}),"newer draft");
    await act(async () => { revision=1; finish({state:state()}); await committed; });
    expect(values.onSaveState).toHaveBeenLastCalledWith("未保存");
    second.unmount();
    render(<CandidateReviewInspector {...values} />);
    expect(screen.getByRole("textbox",{name:"核对反馈"})).toHaveValue("newer draft");
  });
});
