import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { DeliveryVersions } from "./DeliveryVersions";
vi.mock("../../shared/api/client", async original => ({ ...await original<object>(), request: vi.fn() }));
afterEach(()=>{cleanup();vi.resetAllMocks();localStorage.clear();});
const ready={can_lock:true,blockers:[],analysis_target_count:2,effective_conclusion_count:2,ai_default_count:1,human_count:1};
const preview={source_sha256:"s",delivery_sha256:"d",business_basis_sha256:"b",baseline_revision:14,response_revision:2,readiness:ready};
const saved={...preview,revision:1,mode:"FORMAL",created_at:"2026-09-11T00:00:00Z"};
it("binds the explicit lock to the preview and displays the immutable download",async()=>{
  vi.mocked(request).mockImplementation(async (url,options)=>options?.method==="POST"?saved:url.endsWith("preview")?preview:{versions:[]} as never);
  render(<DeliveryVersions projectId="p" documentId="d" sourceSha256="s" baselineRevision={14}/>);
  const user=userEvent.setup();const lock=await screen.findByRole("button",{name:"确认并保存正式版"});
  expect(lock).toBeDisabled();await user.click(screen.getByRole("checkbox"));await user.click(lock);
  expect(await screen.findByRole("link",{name:"下载 Excel"})).toHaveAttribute("href",expect.stringContaining("/versions/1/export"));
  const call=vi.mocked(request).mock.calls.find(([,options])=>options?.method==="POST")!;
  expect(JSON.parse(String(call[1]?.body))).toMatchObject({expected_source_sha256:"s",expected_delivery_sha256:"d",expected_business_basis_sha256:"b",mode:"FORMAL",accept_snapshot:true});
});
it("keeps draft saving available while incomplete responses block formal lock",async()=>{
  vi.mocked(request).mockImplementation(async url=>url.endsWith("preview")?{...preview,readiness:{...ready,can_lock:false,blockers:["仍有 1 个单元待补证据。"]}}:{versions:[]} as never);
  render(<DeliveryVersions projectId="p" documentId="d" sourceSha256="s" baselineRevision={14}/>);
  expect(await screen.findByText("仍有 1 个单元待补证据。")).toBeVisible();
  expect(screen.getByRole("checkbox")).toBeDisabled();expect(screen.getByRole("button",{name:"保存草稿版本"})).toBeEnabled();
});
it("never renders a previous document's download when the next document fails to load",async()=>{
  vi.mocked(request).mockImplementation(async url=>url.endsWith("preview")?preview:{versions:[saved]} as never);
  const view=render(<DeliveryVersions projectId="p" documentId="d" sourceSha256="s" baselineRevision={14}/>);
  expect(await screen.findByRole("link",{name:"下载 Excel"})).toHaveAttribute("href",expect.stringContaining("/sor-documents/d/"));
  vi.mocked(request).mockRejectedValue(new Error("无法读取新文档"));
  view.rerender(<DeliveryVersions projectId="p" documentId="new-doc" sourceSha256="new-source" baselineRevision={1}/>);
  expect(screen.queryByRole("link",{name:"下载 Excel"})).not.toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent("无法读取新文档");
  expect(screen.queryByText(/还没有保存的交付版本/)).not.toBeInTheDocument();
  expect(screen.getByText("暂不可读取")).toBeVisible();
});
