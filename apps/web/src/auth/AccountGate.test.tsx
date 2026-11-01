import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccountGate } from "./AccountGate";
import { AccountRequestError, requestAccountSession } from "./client";

const { csrf, identity } = vi.hoisted(() => ({ csrf: vi.fn(), identity: vi.fn() }));
vi.mock("@reqpilotx/workbench-ui/web", () => ({ setSaasCsrfToken: csrf, setSaasAccountIdentity: identity }));

const guest = { enabled: true, user: null, csrf_token: null };
const user = { enabled: true, user: { id: "account-a", email: "alice@example.invalid" }, csrf_token: "test-csrf-a" };
const fetchMock = vi.fn<typeof fetch>();
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function gate() { return render(<AccountGate><div>private-workspace</div></AccountGate>); }
function fillLogin() {
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: user.user.email } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "fixture-password-long" } });
}

beforeEach(() => {
  fetchMock.mockReset(); csrf.mockReset(); identity.mockReset(); vi.stubGlobal("fetch", fetchMock);
  window.history.replaceState(null, "", "/workspace?projectId=private-a");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("AccountGate", () => {
  it("does not mount private content before the bootstrap session resolves", async () => {
    const request = deferred<Response>(); fetchMock.mockReturnValueOnce(request.promise); gate();
    expect(screen.getByRole("status")).toHaveTextContent("正在检查账户");
    expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
    await act(async () => { request.resolve(response(guest)); });
    expect(await screen.findByRole("heading", { name: "登录私人工作区" })).toBeInTheDocument();
  });

  it("preserves desktop mode only after an explicit disabled response", async () => {
    fetchMock.mockResolvedValueOnce(response({ enabled: false, user: null, csrf_token: null })); gate();
    expect(await screen.findByText("private-workspace")).toBeInTheDocument();
    expect(screen.queryByLabelText("当前账户")).not.toBeInTheDocument();
    expect(csrf).toHaveBeenCalledWith(null);
    expect(identity).toHaveBeenCalledWith(undefined);
  });

  it.each([{}, { enabled: true, user: user.user, csrf_token: null }, { enabled: false, user: user.user, csrf_token: "bad" }])(
    "fails closed on malformed bootstrap data %j", async (value) => {
      fetchMock.mockResolvedValueOnce(response(value)); gate();
      expect(await screen.findByRole("heading", { name: "暂时无法检查账户" })).toBeInTheDocument();
      expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
    },
  );

  it("fails closed on missing auth API and retries without inventing local mode", async () => {
    fetchMock.mockResolvedValueOnce(response({ detail: "not_found" }, 404)).mockResolvedValueOnce(response(guest)); gate();
    fireEvent.click(await screen.findByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "登录私人工作区" })).toBeInTheDocument();
    expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
  });

  it("logs in with same-origin credentials and keeps the deep link", async () => {
    fetchMock.mockResolvedValueOnce(response(guest)).mockResolvedValueOnce(response(user)); gate();
    await screen.findByRole("button", { name: "登录" }); fillLogin(); fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByText("private-workspace")).toBeInTheDocument();
    expect(screen.getByText(user.user.email)).toBeInTheDocument();
    expect(csrf).toHaveBeenCalledWith(user.csrf_token);
    expect(identity).toHaveBeenCalledWith(user.user.id);
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toBe("/api/auth/login");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin", cache: "no-store" });
    expect(JSON.parse(String(init?.body))).toEqual({ email: user.user.email, password: "fixture-password-long" });
    expect(window.location.search).toBe("?projectId=private-a");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it("registers with an invitation and validates the password minimum in the form", async () => {
    fetchMock.mockResolvedValueOnce(response(guest)).mockResolvedValueOnce(response(user)); gate();
    fireEvent.click(await screen.findByRole("button", { name: "有邀请码？创建账户" }));
    const password = screen.getByLabelText("密码（至少 12 位）");
    expect(password).toHaveAttribute("minlength", "12");
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: user.user.email } });
    fireEvent.change(password, { target: { value: "fixture-password-long" } });
    fireEvent.change(screen.getByLabelText("邀请码"), { target: { value: "fixture-invite" } });
    fireEvent.click(screen.getByRole("button", { name: "创建账户" }));
    expect(await screen.findByText("private-workspace")).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][0]).toBe("/api/auth/register");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toHaveProperty("invitation", "fixture-invite");
  });

  it("clears password on failure without exposing server payloads", async () => {
    fetchMock.mockResolvedValueOnce(response(guest)).mockResolvedValueOnce(response({ detail: "arbitrary private provider detail" }, 500)); gate();
    await screen.findByRole("button", { name: "登录" }); fillLogin(); fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法连接账户服务");
    expect(screen.getByLabelText("密码")).toHaveValue("");
    expect(screen.queryByText("arbitrary private provider detail")).not.toBeInTheDocument();
    expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
  });

  it("prevents repeated login submissions while pending", async () => {
    const login = deferred<Response>(); fetchMock.mockResolvedValueOnce(response(guest)).mockReturnValueOnce(login.promise); gate();
    await screen.findByRole("button", { name: "登录" }); fillLogin();
    const form = screen.getByLabelText("密码").closest("form")!;
    fireEvent.submit(form); fireEvent.submit(form);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "请稍候…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "有邀请码？创建账户" })).toBeDisabled();
    await act(async () => { login.resolve(response(user)); });
  });

  it("logs out with CSRF, clears workspace state and removes prior account URL", async () => {
    fetchMock.mockResolvedValueOnce(response(user)).mockResolvedValueOnce(response(guest)); gate();
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));
    expect(await screen.findByRole("heading", { name: "登录私人工作区" })).toBeInTheDocument();
    expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls[1][1]?.headers).toHaveProperty("X-ReqPilot-CSRF", user.csrf_token);
    expect(csrf).toHaveBeenLastCalledWith(null);
    expect(identity).toHaveBeenLastCalledWith(null);
    expect(window.location.pathname + window.location.search).toBe("/");
  });

  it("retains an authenticated workspace and shows error when logout fails", async () => {
    fetchMock.mockResolvedValueOnce(response(user)).mockRejectedValueOnce(new TypeError("offline")); gate();
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法连接账户服务");
    expect(screen.getByText("private-workspace")).toBeInTheDocument();
    expect(csrf).not.toHaveBeenLastCalledWith(null);
  });

  it("treats a logout 401 as an expired session and removes private content", async () => {
    fetchMock.mockResolvedValueOnce(response(user)).mockResolvedValueOnce(response({ detail: "session_expired" }, 401)); gate();
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));
    expect(await screen.findByRole("heading", { name: "登录私人工作区" })).toBeInTheDocument();
    expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
    expect(csrf).toHaveBeenLastCalledWith(null);
  });

  it("unmounts private content and clears CSRF immediately on session expiry", async () => {
    fetchMock.mockResolvedValueOnce(response(user)); gate(); await screen.findByText("private-workspace");
    act(() => { window.dispatchEvent(new Event("reqpilotx:session-expired")); });
    expect(screen.queryByText("private-workspace")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "登录私人工作区" })).toBeInTheDocument();
    expect(csrf).toHaveBeenLastCalledWith(null);
  });

  it("ignores an older refresh after expiry and a newer login", async () => {
    const older = deferred<Response>();
    fetchMock.mockResolvedValueOnce(response(user)).mockReturnValueOnce(older.promise).mockResolvedValueOnce(response(user)); gate();
    await screen.findByText("private-workspace");
    act(() => { window.dispatchEvent(new Event("focus")); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    act(() => { window.dispatchEvent(new Event("reqpilotx:session-expired")); });
    fillLogin(); fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByText("private-workspace");
    await act(async () => { older.resolve(response(guest)); });
    expect(screen.getByText("private-workspace")).toBeInTheDocument();
    expect(csrf).toHaveBeenLastCalledWith(user.csrf_token);
  });

  it("clears the in-memory token when the app unmounts", async () => {
    fetchMock.mockResolvedValueOnce(response(user)); const view = gate(); await screen.findByText("private-workspace"); view.unmount();
    expect(csrf).toHaveBeenLastCalledWith(null);
  });
});

describe("account transport", () => {
  it("redacts an arbitrary failure body to a safe error code", async () => {
    fetchMock.mockResolvedValueOnce(response({ detail: "private-data-and-token=fixture" }, 503));
    await expect(requestAccountSession("session")).rejects.toEqual(new AccountRequestError(503, "account_request_failed"));
  });
  it("rejects authenticated-looking responses without a CSRF token", async () => {
    fetchMock.mockResolvedValueOnce(response({ enabled: true, user: user.user, csrf_token: "" }));
    await expect(requestAccountSession("session")).rejects.toThrow("invalid_session_response");
  });
});
