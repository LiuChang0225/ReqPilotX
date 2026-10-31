export interface AccountSession {
  enabled: boolean;
  user: { id: string; email: string } | null;
  csrf_token: string | null;
}

export class AccountRequestError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super(code);
  }
}

function readSession(value: unknown): AccountSession {
  if (!value || typeof value !== "object") throw new Error("invalid_session_response");
  const item = value as Record<string, unknown>;
  if (typeof item.enabled !== "boolean") throw new Error("invalid_session_response");
  if (item.user !== null) {
    if (!item.user || typeof item.user !== "object") throw new Error("invalid_session_response");
    const user = item.user as Record<string, unknown>;
    if (typeof user.id !== "string" || !user.id || typeof user.email !== "string" || !user.email
      || typeof item.csrf_token !== "string" || !item.csrf_token || !item.enabled) {
      throw new Error("invalid_session_response");
    }
    return { enabled: true, user: { id: user.id, email: user.email }, csrf_token: item.csrf_token };
  }
  if (item.csrf_token !== null) throw new Error("invalid_session_response");
  return { enabled: item.enabled, user: null, csrf_token: null };
}

export async function requestAccountSession(
  action: "session" | "login" | "register" | "logout",
  options: { payload?: Record<string, string>; csrfToken?: string | null; signal?: AbortSignal } = {},
): Promise<AccountSession> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.payload) headers["Content-Type"] = "application/json";
  if (options.csrfToken) headers["X-ReqPilot-CSRF"] = options.csrfToken;
  const response = await fetch(`/api/auth/${action}`, {
    method: action === "session" ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers,
    body: options.payload ? JSON.stringify(options.payload) : undefined,
    signal: options.signal,
  });
  let value: unknown;
  try { value = await response.json(); }
  catch { throw new AccountRequestError(response.status, "invalid_server_response"); }
  if (!response.ok) {
    const detail = value && typeof value === "object" ? (value as Record<string, unknown>).detail : null;
    // Never render arbitrary provider errors or server payloads in the account form.
    const code = typeof detail === "string" && /^[a-z][a-z0-9_]{0,79}$/.test(detail)
      ? detail : "account_request_failed";
    throw new AccountRequestError(response.status, code);
  }
  return readSession(value);
}

export function accountErrorMessage(error: unknown): string {
  if (error instanceof AccountRequestError) {
    if (error.status === 429) return "尝试次数过多，请稍后再试";
    if (error.status === 401) return "邮箱或密码不正确，请重新输入";
    if (error.code.includes("invitation")) return "邀请码无效或暂不可用，请联系邀请人";
    if (error.code.includes("email") || error.code.includes("exists")) return "这个邮箱无法注册，请登录或更换邮箱";
    if (error.status === 422) return "请检查邮箱、密码和邀请码";
    if (error.status === 403) return "请求未获授权，请刷新后再试";
  }
  return "暂时无法连接账户服务，请稍后重试";
}
