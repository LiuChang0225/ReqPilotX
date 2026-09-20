import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { SourceDocumentStageResults } from "./SourceDocumentStageResults";
vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const sha="a".repeat(64), map="b".repeat(64);
const props={root:"/extraction-loop",taskId:"task",sourceSha256:sha,
  stages:[{stage:"MAP" as const,artifact_sha256:map,count:1}],confirmationRequired:true,onConfirmed:vi.fn()};
const preview={source_sha256:sha,artifact_sha256:map,total:1,next_offset:null,rows:[{node_id:"contents",kind:"TABLE_OF_CONTENTS",title:"目录",
  pages:[1],ancestor_path:[],source_content:[{text:"1.1 接口……2\n1.2 通信……3"}]}]};
describe("Reviewable native stage results", () => {
 it("shows complete contents as one block before source items and confirms that exact Map", async () => {
   vi.mocked(request).mockResolvedValueOnce(preview).mockResolvedValueOnce({task_id:"task",job_id:"continued"});
   render(<SourceDocumentStageResults {...props}/>);
   fireEvent.click(screen.getByRole("button",{name:/查看文档结构/}));
   await screen.findByText("目录");
   fireEvent.click(screen.getByText("目录"));
   expect(screen.getByText(/1.1 接口/)).toHaveTextContent("1.2 通信");
   expect(screen.queryByText(/SRC-/)).not.toBeInTheDocument();
   fireEvent.click(screen.getByRole("button",{name:"确认此结构并继续"}));
   await waitFor(()=>expect(props.onConfirmed).toHaveBeenCalledWith({task_id:"task",job_id:"continued"}));
   expect(JSON.parse(vi.mocked(request).mock.calls[1][1]!.body as string)).toMatchObject({expected_source_sha256:sha,expected_map_sha256:map});
 });
 it("does not enable confirmation of a different source or Map version",async()=>{
   vi.mocked(request).mockResolvedValue({...preview,artifact_sha256:"c".repeat(64)});
   render(<SourceDocumentStageResults {...props}/>);
   fireEvent.click(screen.getByRole("button",{name:/查看文档结构/}));
   await screen.findByRole("alert");
   expect(screen.queryByRole("button",{name:"确认此结构并继续"})).not.toBeInTheDocument();
 });
});

it("keeps requirement identity and classification visible in the main ITEMS surface", async () => {
  vi.mocked(request).mockResolvedValue({ ...preview, rows: [{ source_record_id: "item", source_display_id: "SRC-0001", requirement_display_id: "REQ-0001", title: "控制功能", pages: [2], requirementness: "REQUIREMENT", requirement_kinds: ["FUNCTIONAL"], source_text: "保留完整客户原文" }] });
  render(<SourceDocumentStageResults {...props} stages={[{ stage: "ITEMS", artifact_sha256: map, count: 1 }]} initialStage="ITEMS" workspace confirmationRequired={false} />);
  await screen.findByText("REQ-0001");
  expect(screen.getByText("需求")).toBeInTheDocument();
  expect(screen.getByText("功能")).toBeInTheDocument();
  expect(screen.getByText("保留完整客户原文")).toBeInTheDocument();
});
