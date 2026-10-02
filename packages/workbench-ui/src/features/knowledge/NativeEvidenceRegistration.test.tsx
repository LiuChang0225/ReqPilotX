import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { request } from "../../shared/api/client";
import { NativeEvidenceRegistration } from "./NativeEvidenceRegistration";
import type { EvidenceUploadResult, OpenKbEvidenceStatus } from "./openKbEvidenceApi";

vi.mock("../../shared/api/client", () => ({ request: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(request).mockReset(); });

const status: OpenKbEvidenceStatus = {
  available: true, knowledge_base_version: 7, catalog_sha256: "c".repeat(64),
  generation: `generation-${"d".repeat(64)}`, live_openkb: "NOT_RUN", runtime_available: false,
};
function receipt(names: string[], failed: string[] = []): EvidenceUploadResult {
  return {
    documents: names.map((file_name, index) => failed.includes(file_name)
      ? { file_name, status: "FAILED", message: "原件格式暂不可读取" }
      : { file_name, status: "REGISTERED", document_id: `registered-${index}` }),
    registered_count: names.length - failed.length, failed_count: failed.length, knowledge_base_version: 8,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function installTransport(upload: (init: RequestInit) => Promise<unknown> = () => Promise.resolve(receipt(["source.pdf"]))) {
  vi.mocked(request).mockImplementation((path, init = {}) => {
    if (path.endsWith("/status")) return Promise.resolve(status) as ReturnType<typeof request>;
    if (path.endsWith("/upload")) return upload(init) as ReturnType<typeof request>;
    throw new Error(`Unexpected request: ${path}`);
  });
}
async function expand() {
  const details = screen.getByText("登记项目证据原件").closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(screen.getByLabelText("选择证据文件")).toBeEnabled());
}
function choose(files: File[], folder = false) {
  fireEvent.change(screen.getByLabelText(folder ? "选择证据文件夹" : "选择证据文件"), { target: { files } });
}
function uploads() { return vi.mocked(request).mock.calls.filter(([path]) => path.endsWith("/upload")); }

describe("native evidence registration", () => {
  it("loads status only when expanded and uploads actual selected files with explicit metadata", async () => {
    installTransport(() => Promise.resolve(receipt(["source.pdf", "notes.txt"])));
    const onRegistered = vi.fn();
    render(<NativeEvidenceRegistration projectId="project / one" onRegistered={onRegistered} />);
    expect(request).not.toHaveBeenCalled();
    await expand();
    expect(request).toHaveBeenCalledWith("/api/projects/project%20%2F%20one/openkb-evidence/status", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    const files = [new File(["PDF bytes"], "source.pdf", { type: "application/pdf" }), new File(["Actual notes"], "notes.txt")];
    choose(files);
    fireEvent.change(screen.getByLabelText("资料类型"), { target: { value: "STANDARD" } });
    fireEvent.change(screen.getByLabelText("来源权威"), { target: { value: "CUSTOMER_APPROVED" } });
    fireEvent.change(screen.getByLabelText("证据用途"), { target: { value: "TEST_RESULT" } });
    fireEvent.change(screen.getByLabelText("适用性"), { target: { value: "适用于 A 子系统" } });
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    await screen.findByText(/原件登记完成：2 成功，0 失败/);
    const [path, init] = uploads()[0];
    expect(path).toBe("/api/projects/project%20%2F%20one/openkb-evidence/upload");
    expect(init?.method).toBe("POST");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    const form = init?.body as FormData;
    expect(form.getAll("files")).toEqual(files);
    expect(form.get("file")).toBeNull();
    expect(Object.fromEntries(["doc_type", "authority", "claim_role", "applicability"].map(key => [key, form.get(key)]))).toEqual({
      doc_type: "STANDARD", authority: "CUSTOMER_APPROVED", claim_role: "TEST_RESULT", applicability: "适用于 A 子系统",
    });
    expect(onRegistered).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("list", { name: "待登记证据文件" })).not.toBeInTheDocument();
    expect(screen.getByText(/登记成功不表示已证明符合性/)).toBeInTheDocument();
    expect(screen.getByText(/OpenKB 实时检索未运行.*检索运行环境不可用/)).toBeInTheDocument();
    expect(vi.mocked(request).mock.calls.filter(([url]) => url.endsWith("/status"))).toHaveLength(2);
  });

  it("keeps folder-relative names as multipart labels and defaults unknown metadata", async () => {
    installTransport(() => Promise.resolve(receipt(["test-records/source.pdf"])));
    render(<NativeEvidenceRegistration projectId="p" onRegistered={vi.fn()} />);
    await expand();
    const file = new File(["folder bytes"], "source.pdf");
    Object.defineProperty(file, "webkitRelativePath", { value: "test-records/source.pdf" });
    expect(screen.getByLabelText("选择证据文件夹")).toHaveAttribute("webkitdirectory", "");
    choose([file], true);
    expect(within(screen.getByRole("list", { name: "待登记证据文件" })).getByText("test-records/source.pdf")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    await screen.findByText(/原件登记完成/);
    const form = uploads()[0][1]?.body as FormData;
    expect((form.getAll("files")[0] as File).name).toBe("test-records/source.pdf");
    expect((form.getAll("files")[0] as File).size).toBe(file.size);
    expect(form.get("doc_type")).toBe("REFERENCE");
    expect(form.get("authority")).toBe("UNKNOWN");
    expect(form.get("claim_role")).toBe("UNKNOWN");
    expect(form.get("applicability")).toBe("UNKNOWN");
    expect(form.get("path")).toBeNull();
  });

  it("reports partial success and retries only failed originals", async () => {
    let attempt = 0;
    installTransport(() => Promise.resolve(++attempt === 1 ? receipt(["ok.pdf", "folder/retry.pdf"], ["folder/retry.pdf"]) : receipt(["folder/retry.pdf"])));
    const onRegistered = vi.fn();
    render(<NativeEvidenceRegistration projectId="p" onRegistered={onRegistered} />);
    await expand();
    const success = new File(["ok"], "ok.pdf"), retry = new File(["retry"], "retry.pdf");
    Object.defineProperty(retry, "webkitRelativePath", { value: "folder/retry.pdf" });
    choose([success, retry], true);
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    await screen.findByText(/部分原件已登记：1 成功，1 失败/);
    const remaining = screen.getByRole("list", { name: "待登记证据文件" });
    expect(within(remaining).queryByText("ok.pdf")).not.toBeInTheDocument();
    expect(within(remaining).getByText("folder/retry.pdf")).toBeInTheDocument();
    expect(screen.getByText("ok.pdf：已登记")).toBeInTheDocument();
    expect(screen.getByText(/folder\/retry.pdf：未登记.*原件格式暂不可读取/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试未登记文件" }));
    await screen.findByText(/原件登记完成：1 成功，0 失败/);
    expect((uploads()[1][1]?.body as FormData).getAll("files").map(file => (file as File).name)).toEqual(["folder/retry.pdf"]);
    expect(onRegistered).toHaveBeenCalledTimes(2);
  });

  it("reports total failure without refreshing or dropping the selected file", async () => {
    installTransport(() => Promise.resolve(receipt(["source.pdf"], ["source.pdf"])));
    const onRegistered = vi.fn();
    render(<NativeEvidenceRegistration projectId="p" onRegistered={onRegistered} />);
    await expand(); choose([new File(["source"], "source.pdf")]);
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("本次原件未登记：0 成功，1 失败");
    expect(screen.getByRole("list", { name: "待登记证据文件" })).toHaveTextContent("source.pdf");
    expect(screen.getByRole("button", { name: "重试未登记文件" })).toBeEnabled();
    expect(onRegistered).not.toHaveBeenCalled();
  });

  it.each([
    ["network failure", () => Promise.reject(new Error("连接中断")), "连接中断"],
    ["incomplete receipt", () => Promise.resolve({ ...receipt(["source.pdf"]), registered_count: 2 }), "登记回执不完整"],
  ])("retains files and reconciliation guidance after %s", async (_label, upload, message) => {
    installTransport(upload);
    const onRegistered = vi.fn();
    render(<NativeEvidenceRegistration projectId="p" onRegistered={onRegistered} />);
    await expand(); choose([new File(["source"], "source.pdf")]);
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("alert")).toHaveTextContent("已选文件仍保留");
    expect(screen.getByRole("alert")).toHaveTextContent("先同步资料");
    expect(screen.getByRole("list", { name: "待登记证据文件" })).toHaveTextContent("source.pdf");
    expect(screen.getByRole("button", { name: "登记所选原件" })).toBeEnabled();
    expect(onRegistered).not.toHaveBeenCalled();
    expect(screen.queryByText(/原件登记完成/)).not.toBeInTheDocument();
  });

  it("rejects a wrong-name receipt without dropping the selected original from a retry", async () => {
    let attempt = 0;
    installTransport(() => Promise.resolve(++attempt === 1
      ? receipt(["other-folder/source.pdf"], ["other-folder/source.pdf"])
      : receipt(["selected-folder/source.pdf"])));
    const onRegistered = vi.fn();
    render(<NativeEvidenceRegistration projectId="p" onRegistered={onRegistered} />);
    await expand();
    const selected = new File(["selected original bytes"], "source.pdf");
    Object.defineProperty(selected, "webkitRelativePath", { value: "selected-folder/source.pdf" });
    choose([selected], true);
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("登记回执不完整");
    expect(screen.getByRole("alert")).toHaveTextContent("请同步资料后核对再重试");
    expect(screen.getByRole("list", { name: "待登记证据文件" })).toHaveTextContent("selected-folder/source.pdf");
    expect(screen.queryByText(/other-folder\/source.pdf/)).not.toBeInTheDocument();
    expect(onRegistered).not.toHaveBeenCalled();
    expect(screen.queryByText(/原件登记完成|本次原件未登记/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    await screen.findByText(/原件登记完成：1 成功，0 失败/);
    expect((uploads()[1][1]?.body as FormData).getAll("files").map(file => (file as File).name)).toEqual(["selected-folder/source.pdf"]);
    expect(onRegistered).toHaveBeenCalledTimes(1);
  });

  it("aborts interrupted waiting and warns that server registration may already have happened", async () => {
    const pending = deferred<unknown>();
    installTransport(init => {
      init.signal?.addEventListener("abort", () => pending.reject(new DOMException("aborted", "AbortError")), { once: true });
      return pending.promise;
    });
    const onRegistered = vi.fn();
    render(<NativeEvidenceRegistration projectId="p" onRegistered={onRegistered} />);
    await expand(); choose([new File(["source"], "source.pdf")]);
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    expect(screen.getByLabelText("选择证据文件")).toBeDisabled();
    expect(screen.getByLabelText("资料类型")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "停止等待" }));
    expect(uploads()[0][1]?.signal?.aborted).toBe(true);
    expect(await screen.findByRole("alert")).toHaveTextContent("已停止等待。服务端可能已登记部分原件，请同步资料核对后再重试。");
    expect(screen.getByRole("list", { name: "待登记证据文件" })).toHaveTextContent("source.pdf");
    expect(screen.getByRole("button", { name: "登记所选原件" })).toBeEnabled();
    expect(onRegistered).not.toHaveBeenCalled();
  });

  it("gates repeated synchronous submits before React can render the pending state", async () => {
    const pending = deferred<unknown>(); installTransport(() => pending.promise);
    render(<NativeEvidenceRegistration projectId="p" onRegistered={vi.fn()} />);
    await expand(); choose([new File(["source"], "source.pdf")]);
    const form = screen.getByRole("button", { name: "登记所选原件" }).closest("form")!;
    act(() => { fireEvent.submit(form); fireEvent.submit(form); fireEvent.submit(form); });
    expect(uploads()).toHaveLength(1);
    await act(async () => pending.resolve(receipt(["source.pdf"])));
    expect(await screen.findByText(/原件登记完成：1 成功，0 失败/)).toBeInTheDocument();
  });

  it("aborts and discards old uploads on a keyed project remount", async () => {
    const pending = deferred<unknown>(); installTransport(() => pending.promise);
    const onRegistered = vi.fn();
    const { rerender } = render(<NativeEvidenceRegistration key="old" projectId="old" onRegistered={onRegistered} />);
    await expand(); choose([new File(["old source"], "old-source.pdf")]);
    fireEvent.click(screen.getByRole("button", { name: "登记所选原件" }));
    const signal = uploads()[0][1]?.signal;
    rerender(<NativeEvidenceRegistration key="new" projectId="new" onRegistered={onRegistered} />);
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(receipt(["old-source.pdf"])));
    await expand();
    expect(screen.queryByText(/old-source.pdf/)).not.toBeInTheDocument();
    expect(screen.queryByText(/原件登记完成/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "登记所选原件" })).toBeDisabled();
    expect(onRegistered).not.toHaveBeenCalled();
    expect(request).toHaveBeenCalledWith("/api/projects/new/openkb-evidence/status", expect.anything());
  });

  it("can retry a failed status read without uploading any file", async () => {
    vi.mocked(request).mockRejectedValueOnce(new Error("资料状态请求失败")).mockResolvedValue(status);
    render(<NativeEvidenceRegistration projectId="p" onRegistered={vi.fn()} />);
    const details = screen.getByText("登记项目证据原件").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(await screen.findByRole("alert")).toHaveTextContent("资料状态请求失败");
    expect(screen.getByLabelText("选择证据文件")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试资料状态" }));
    await waitFor(() => expect(screen.getByLabelText("选择证据文件")).toBeEnabled());
    expect(uploads()).toHaveLength(0);
  });

  it("keeps unavailable registration disabled even when the status response is valid", async () => {
    vi.mocked(request).mockResolvedValue({ ...status, available: false });
    render(<NativeEvidenceRegistration projectId="p" onRegistered={vi.fn()} />);
    const details = screen.getByText("登记项目证据原件").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(await screen.findByRole("alert")).toHaveTextContent("本项目的来源登记入口当前不可用");
    expect(screen.getByLabelText("选择证据文件")).toBeDisabled();
    expect(screen.getByRole("button", { name: "登记所选原件" })).toBeDisabled();
    expect(uploads()).toHaveLength(0);
  });

  it("shows the project prerequisite and never reads status with an empty project", () => {
    render(<NativeEvidenceRegistration projectId="" onRegistered={vi.fn()} />);
    const details = screen.getByText("登记项目证据原件").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(screen.getByText("先选择项目，再登记原件。")).toBeInTheDocument();
    expect(screen.queryByLabelText("选择证据文件")).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });
});
